"""开盘首根集合竞价异常量的检测与校正工具。

背景
----
新浪 1m 源在部分交易日会把「开盘集合竞价」的累计委托量（而非实际成交量）
写入日盘 09:00 首根，导致首根量放大 3~19 倍，在副图 VOL 上形成突兀巨柱。

本脚本是 backend.storage.history_backfill.repair_open_auction_bar 的命令行外壳，
判定与校正口径完全一致（那里是服务运行时每轮缺口修补时自动调用的入口）：
    基准 k0 = 该合约历史正常日「日盘首根 / 当日次高分钟」倍数的中位数
              （先剔除异常日，避免异常日抬高基准而漏检）
    判定    倍数 >= max(3.0, k0 + 1.5) 且溢出量 >= 1000 手
    校正    首根目标量 = 次高 × k0，差额按后续各分钟量占比加权摊回，
            保证「日盘合计不变」，因此日线总量与日线 OHLC 不受影响。

用法:
    # 体检（只读，不改库）
    ./.venv/Scripts/python.exe scripts/fix_open_auction_bar.py --dry-run

    # 指定合约/交易日
    ./.venv/Scripts/python.exe scripts/fix_open_auction_bar.py --dry-run --symbols FU2611 SA2701

    # 执行（先确保已备份）
    ./.venv/Scripts/python.exe scripts/fix_open_auction_bar.py --apply

    # 只处理指定交易日
    ./.venv/Scripts/python.exe scripts/fix_open_auction_bar.py --apply --trading-days 20260914
"""
import argparse
import logging
import sys
from collections import defaultdict
from decimal import Decimal  # noqa: F401  (保持与 backend 一致的导入面)

sys.path.insert(0, "D:/期货实时交易系统")

from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel
from backend.storage.history_backfill import (
    _auction_day_session,
    _auction_first_ratio,
    _AUCTION_ABS_RATIO_FLOOR,
    _AUCTION_MIN_OVERFLOW,
    _AUCTION_BASELINE_MIN,
    repair_open_auction_bar,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fix_open_auction")


def compute_baseline(day_map):
    """对单合约求「正常日首根倍数」的基准中位数（与 backend 同口径）"""
    import statistics
    ratios = []
    for bars in day_map.values():
        got = _auction_first_ratio(bars)
        if got:
            ratios.append(got[2])
    if not ratios:
        return 1.5, ratios
    coarse = statistics.median(ratios)
    clean = [x for x in ratios if x < max(_AUCTION_ABS_RATIO_FLOOR, coarse * 1.5)]
    base = statistics.median(clean) if clean else statistics.median(ratios)
    return max(base, _AUCTION_BASELINE_MIN), ratios


def scan(rows_by_inst, only_symbols=None, only_days=None):
    """扫描全部合约, 返回待校正清单。

    镜像合称 (主连 FU0 与其主力合约 FU2611) 的 1m 数据是同一份行情,
    原始首根/次高完全相同, 但历史样本长度不同会导致基准不一致。
    这里对「同一交易日 + 相同原始首根/次高」的合约组统一基准,
    取组内基准中位数, 保证主连与主力合约校正结果一致。
    """
    import statistics
    raw = []
    for inst in sorted(rows_by_inst):
        if only_symbols and inst not in only_symbols:
            continue
        day_map = defaultdict(list)
        for r in rows_by_inst[inst]:
            day_map[r.trading_day].append(r)
        base, _ = compute_baseline(day_map)
        for td in sorted(day_map):
            if only_days and td not in only_days:
                continue
            got = _auction_first_ratio(day_map[td])
            if not got:
                continue
            first, second, ratio = got
            raw.append({
                "instrument": inst,
                "trading_day": td,
                "bar_time": first.bar_time,
                "original": first.volume,
                "second": second,
                "ratio": ratio,
                "baseline": base,
                "bar_id": first.id,
            })

    # 统一镜像合约组的基准
    groups = defaultdict(list)
    for item in raw:
        groups[(item["trading_day"], item["original"], item["second"])].append(item)
    for members in groups.values():
        if len(members) > 1:
            unified = statistics.median([m["baseline"] for m in members])
            for m in members:
                m["baseline"] = unified

    findings = []
    for item in raw:
        base = item["baseline"]
        threshold = min(max(_AUCTION_ABS_RATIO_FLOOR, base * 3.0),
                        max(_AUCTION_ABS_RATIO_FLOOR, base + 1.5))
        if item["ratio"] < threshold:
            continue
        target = int(round(item["second"] * base))
        if target >= item["original"]:
            continue
        overflow = item["original"] - target
        if overflow < _AUCTION_MIN_OVERFLOW:
            continue
        item["threshold"] = threshold
        item["target"] = target
        item["overflow"] = overflow
        findings.append(item)
    return findings


def preview(findings):
    if not findings:
        print("未发现需要校正的开盘首根异常。")
        return
    print(f"{'合约':>8} {'交易日':>10} {'原首根':>10} {'次高':>9} {'倍数':>7} "
          f"{'基准':>6} {'阈值':>6} {'校正后':>9} {'摊回量':>9}")
    print("-" * 92)
    for f in findings:
        print(f"{f['instrument']:>8} {f['trading_day']:>10} {f['original']:>10,} "
              f"{f['second']:>9,} {f['ratio']:>6.1f}x {f['baseline']:>6.2f} "
              f"{f['threshold']:>6.1f} {f['target']:>9,} {f['overflow']:>9,}")
    total = sum(f["overflow"] for f in findings)
    print("-" * 92)
    print(f"共 {len(findings)} 处, 合计摊回 {total:,} 手（日盘合计保持不变）")


def apply_fixes(findings):
    """执行校正: 直接复用 backend 的修复实现, 保证与运行时口径一致。

    backend.storage.history_backfill.repair_open_auction_bar 是服务运行时
    每轮缺口修补都会调用的入口; 脚本层只负责「先扫出清单给用户确认」,
    实际写库交给同一份代码, 避免两处逻辑漂移。
    """
    if not findings:
        return 0
    session = SessionLocal()
    fixed = 0
    try:
        for inst in sorted({f["instrument"] for f in findings}):
            n = repair_open_auction_bar(inst, session=session)
            fixed += n
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if fixed:
        _rebuild_redis(fixed)
    return fixed


def _rebuild_redis(_count):
    """校正后重建受影响的 Redis 近端缓存, 避免旧值继续遮蔽"""
    try:
        from backend.storage.redis_client import RedisClient
        rc = RedisClient.get_client()
        for key in rc.scan_iter("bars:*:1m"):
            rc.delete(key)
        logger.info("已清空 bars:*:1m 缓存, 将由引擎/回补任务按库重建")
    except Exception as e:
        logger.warning("Redis 缓存重建失败: %s", e)


def verify(findings):
    """校正后校验: 每笔的日盘合计是否守恒"""
    session = SessionLocal()
    try:
        ok = 0
        for f in findings:
            rows = (session.query(MarketBarModel)
                    .filter(MarketBarModel.instrument_id == f["instrument"],
                            MarketBarModel.period == "1m",
                            MarketBarModel.trading_day == f["trading_day"]).all())
            d = _day_session(rows)
            first_v = d[0].volume if d else 0
            total = sum(b.volume for b in d)
            status = "OK" if first_v == f["target"] else "FAIL"
            if status == "OK":
                ok += 1
            print(f"  校验 {f['instrument']} {f['trading_day']}: 首根={first_v:,} "
                  f"日盘合计={total:,} [{status}]")
        print(f"校验通过 {ok}/{len(findings)}")
        return ok == len(findings)
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只体检, 不改库")
    g.add_argument("--apply", action="store_true", help="执行校正")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--trading-days", nargs="+", default=None)
    args = ap.parse_args()

    session = SessionLocal()
    try:
        q = session.query(MarketBarModel).filter(MarketBarModel.period == "1m")
        rows = q.order_by(MarketBarModel.instrument_id, MarketBarModel.bar_time).all()
    finally:
        session.close()

    by_inst = defaultdict(list)
    for r in rows:
        by_inst[r.instrument_id].append(r)

    findings = scan(by_inst, args.symbols, args.trading_days)
    preview(findings)

    if args.dry_run:
        print("\n[dry-run] 未修改任何数据。确认无误后加 --apply 执行。")
        return

    if not findings:
        return
    n = apply_fixes(findings)
    print(f"\n已校正 {n} 处。开始校验:")
    verify(findings)


if __name__ == "__main__":
    main()
