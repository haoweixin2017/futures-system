"""修复 1m K 线时间戳口径错乱 (收盘幽灵 bar + 新浪未平移存量)。

背景
----
新浪分钟线的时间戳是「分钟结束时刻」(夜盘 21:01~23:00), 本地 BarGenerator 用「分钟开始
时刻」(21:00~22:59)。历史回补早期版本未做 -1m 平移, 导致两类脏数据:

  1. 幽灵 bar —— bar_time 恰为收盘整点 (15:00:00 / 23:00:00 / 01:00:00 等),
     不属于任何可交易分钟, 是收盘静态快照的残影。
  2. 整体错位 —— 整段夜盘/日盘右移一分钟 (21:01~23:00 而非 21:00~22:59),
     使前端固定交易时段槽位与数据错位, 副图 VOL 出现空柱+尾部多柱。

本脚本对指定合约做「先删后补」: 清掉收官整点前的错位尾部, 再由 repair_minute_gaps
按正确口径 (+ -1m) 重灌, 保证 21:00~22:59 / 09:00~14:59 对齐。

用法
----
    python scripts/fix_minute_alignment.py --dry-run          # 只体检, 不改动
    python scripts/fix_minute_alignment.py --apply            # 实际修复
    python scripts/fix_minute_alignment.py --apply --symbols FU2611,FU2610
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.common.config import cfg
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel
from backend.storage.redis_client import RedisClient

# 幽灵 bar 判据只适用于 1m: 本地 1m 用「分钟开始」标记, 收盘整点 (15:00/23:00) 不属于
# 任何可交易分钟, 只能是收盘快照残影。
# 注意高阶周期 (5m/15m/30m/60m) 的时间戳语义在本项目内并不统一 (5m 偏区间结束,
# 15m/30m/60m 用法各异), 其 15:00 多为正常区间标记, 不能按同一规则误删。
CLOSE_HOURS = (15, 23)


def _is_ghost(bar_time, instrument: str) -> bool:
    """仅 1m: bar_time 恰为收盘整点且不是该合约的可交易分钟。"""
    from backend.common.trading_time import FuturesTradingCalendar
    return bar_time.minute == 0 and not FuturesTradingCalendar.is_trading_time_for(bar_time, instrument)


def diagnose(symbol: str, session) -> Dict:
    """体检单合约 1m 数据: 幽灵 bar 数、夜盘起始口径、尾部口径。"""
    rows = (session.query(MarketBarModel)
            .filter(MarketBarModel.instrument_id == symbol,
                    MarketBarModel.period == "1m")
            .order_by(MarketBarModel.bar_time).all())
    if not rows:
        return {"symbol": symbol, "bars": 0}

    ghosts = [r for r in rows if _is_ghost(r.bar_time, symbol)]
    # 夜盘起始口径: 统计 21:00 与 21:01 的出现次数
    h2100 = sum(1 for r in rows if r.bar_time.hour == 21 and r.bar_time.minute == 0)
    h2101 = sum(1 for r in rows if r.bar_time.hour == 21 and r.bar_time.minute == 1)
    misaligned = h2101 > h2100

    return {
        "symbol": symbol, "bars": len(rows),
        "ghosts": len(ghosts),
        "ghost_vol": sum(int(r.volume or 0) for r in ghosts),
        "night_2100": h2100, "night_2101": h2101,
        "misaligned": misaligned,
        "first": rows[0].bar_time, "last": rows[-1].bar_time,
    }


def delete_ghosts(session, symbols: List[str]) -> int:
    """删除 bar_time 为收盘整点的幽灵 bar。"""
    removed = 0
    for sym in symbols:
        rows = (session.query(MarketBarModel)
                .filter(MarketBarModel.instrument_id == sym,
                        MarketBarModel.period == "1m").all())
        for r in rows:
            if _is_ghost(r.bar_time, sym):
                session.delete(r)
                removed += 1
    session.commit()
    return removed


def find_misaligned_days(session, symbol: str) -> List[datetime]:
    """找出该合约哪些「夜盘起始」是未平移口径 (21:01 而非 21:00)。

    返回这些日期 (bar_time 的自然日) 列表 —— 这些天需要整段删除后重灌。
    """
    rows = (session.query(MarketBarModel)
            .filter(MarketBarModel.instrument_id == symbol,
                    MarketBarModel.period == "1m",
                    MarketBarModel.bar_time.like("% 21:01:00"))
            .all())
    return sorted({r.bar_time.replace(hour=0, minute=0, second=0, microsecond=0) for r in rows})


def purge_misaligned_range(session, symbol: str) -> Tuple[int, int]:
    """删除未平移夜盘的整段错位数据 (当日 21:00 起至次日收盘)。

    判据: 该自然日夜盘起始戳为 21:01 (未平移), 则删除 21:01~23:00 及次日 00:00~15:00
    全部 1m 行, 交由 repair_minute_gaps 按正确口径重灌。
    返回 (删除行数, 涉及天数)。
    """
    days = find_misaligned_days(session, symbol)
    removed = 0
    for day in days:
        # 夜盘: 当日 21:00 起至次日凌晨; 日盘: 次日至再次日 15:00
        start = day.replace(hour=21, minute=0)
        end = day + timedelta(days=2)
        rows = (session.query(MarketBarModel)
                .filter(MarketBarModel.instrument_id == symbol,
                        MarketBarModel.period == "1m",
                        MarketBarModel.bar_time >= start,
                        MarketBarModel.bar_time < end).all())
        for r in rows:
            session.delete(r)
            removed += 1
    session.commit()
    return removed, len(days)


def detect_misaligned_symbols(session) -> List[str]:
    """全库扫描: 找出夜盘仍用 21:01 起始戳的合约。"""
    out = []
    syms = [r[0] for r in session.query(MarketBarModel.instrument_id)
            .filter(MarketBarModel.period == "1m").distinct().all()]
    for sym in syms:
        h2100 = (session.query(MarketBarModel)
                 .filter(MarketBarModel.instrument_id == sym,
                         MarketBarModel.period == "1m",
                         MarketBarModel.bar_time.like("% 21:00:00")).count())
        h2101 = (session.query(MarketBarModel)
                 .filter(MarketBarModel.instrument_id == sym,
                         MarketBarModel.period == "1m",
                         MarketBarModel.bar_time.like("% 21:01:00")).count())
        if h2101 > h2100:
            out.append(sym)
    return sorted(out)


def detect_lag_residue(session, symbol: str, sample: int = 400) -> Tuple[int, int]:
    """检测「整体滞后一格」的历史残留 (DB[t] 等于新浪源 t-1 的值)。

    返回 (滞后吻合根数, 可比根数)。仅统计收盘前的已结算历史, 用于判定该合约
    的历史存量是否需要整段重灌。需联网访问新浪, 失败时返回 (0, 0)。
    """
    try:
        from backend.storage.history_backfill import fetch_sina_minute_bars
        src = {b["bar_time"]: int(b["volume"]) for b in fetch_sina_minute_bars(symbol, "1m")}
    except Exception:
        return 0, 0
    if not src:
        return 0, 0
    rows = (session.query(MarketBarModel)
            .filter(MarketBarModel.instrument_id == symbol,
                    MarketBarModel.period == "1m")
            .order_by(MarketBarModel.bar_time).all())
    cmpable = lagged = 0
    for r in rows[:sample]:
        v = int(r.volume or 0)
        if r.bar_time in src:
            cmpable += 1
            if src.get(r.bar_time - timedelta(minutes=1)) == v and src.get(r.bar_time) != v:
                lagged += 1
    return lagged, cmpable


def find_lag_range(session, symbol: str) -> Tuple[datetime, datetime, int]:
    """定位该合约「整体滞后一格」残留的连续时间范围。

    逐根比对 DB 与新浪源: 判定 DB[t] 是「对齐」还是「滞后一格」, 找出首个和最后一个
    滞后根, 返回 (起始, 结束, 滞后根数)。与新浪不符的根视为需重灌。
    需联网访问新浪; 无法比对时返回 (None, None, 0)。
    """
    try:
        from backend.storage.history_backfill import fetch_sina_minute_bars
        src = {b["bar_time"]: float(b["close_price"])
               for b in fetch_sina_minute_bars(symbol, "1m")}
    except Exception:
        return None, None, 0
    if not src:
        return None, None, 0

    rows = (session.query(MarketBarModel)
            .filter(MarketBarModel.instrument_id == symbol,
                    MarketBarModel.period == "1m")
            .order_by(MarketBarModel.bar_time).all())
    bad: List[datetime] = []
    for r in rows:
        t = r.bar_time
        if t not in src:
            continue
        v = float(r.close_price or 0)
        if abs(v - src[t]) < 1e-6:
            continue  # 与源同刻一致 -> 正常
        prev = t - timedelta(minutes=1)
        if prev in src and abs(v - src[prev]) < 1e-6:
            bad.append(t)  # 等于源前一格 -> 滞后
        else:
            bad.append(t)  # 与源完全不符 -> 同样需重灌
    if not bad:
        return None, None, 0
    return min(bad), max(bad), len(bad)


def purge_range(session, symbol: str, start: datetime, end: datetime) -> int:
    """删除 [start, end] 区间的 1m 行, 交由 repair_minute_gaps 按正确口径重灌。

    按整交易日边界扩张: 向前扩到当日 21:00 (夜盘起点), 向后扩到次日 15:00 (收盘),
    避免留下半截夜盘与日盘错配。
    """
    lo = start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
    hi = end.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(hours=16)
    rows = (session.query(MarketBarModel)
            .filter(MarketBarModel.instrument_id == symbol,
                    MarketBarModel.period == "1m",
                    MarketBarModel.bar_time >= lo,
                    MarketBarModel.bar_time <= hi).all())
    for r in rows:
        session.delete(r)
    session.commit()
    return len(rows)


def rebuild_redis(symbol: str, session):
    """用 MySQL 里已修正的 1m 数据重建 Redis 近端缓存。"""
    from backend.storage.history_backfill import _rebuild_minute_redis_cache
    _rebuild_minute_redis_cache(symbol, datetime.now())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只体检不改动")
    ap.add_argument("--apply", action="store_true", help="实际执行修复")
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔合约; 缺省=自动扫描全部")
    ap.add_argument("--rebackfill", action="store_true",
                    help="清除错位数据后按正确口径重灌 (需联网访问新浪)")
    ap.add_argument("--lagscan", action="store_true",
                    help="额外扫描「整体滞后一格」的历史残留 (需联网访问新浪)")
    ap.add_argument("--fixlag", action="store_true",
                    help="修复滞后/不符残留: 删区间 + 按正确口径重灌 (需联网访问新浪)")
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        ap.error("必须指定 --dry-run 或 --apply")

    session = SessionLocal()
    try:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = [r[0] for r in session.query(MarketBarModel.instrument_id)
                       .filter(MarketBarModel.period == "1m").distinct().all()]

        print("=" * 82)
        print("体检: 1m 时间戳口径")
        print("=" * 82)
        misaligned = []
        for sym in sorted(symbols):
            d = diagnose(sym, session)
            if d.get("bars", 0) == 0:
                continue
            flag = "  <<< 错位" if d["misaligned"] else ""
            print(f"  {sym:10} {d['bars']:5}根 {d['first']} ~ {d['last']}  "
                  f"幽灵{d['ghosts']}根 21:00={d['night_2100']} 21:01={d['night_2101']}{flag}")
            if d["misaligned"]:
                misaligned.append(sym)

        print(f"\n  疑似错位合约 ({len(misaligned)}): {misaligned}")

        if args.lagscan:
            print("\n" + "=" * 82)
            print("扫描: 整体滞后一格的历史残留 (DB[t] == 源[t-1])")
            print("=" * 82)
            for sym in sorted(symbols):
                lag, cmp_ = detect_lag_residue(session, sym)
                if cmp_ and lag / cmp_ > 0.5:
                    print(f"  !! {sym:10} 滞后吻合 {lag}/{cmp_} ({lag/cmp_*100:.0f}%) 需整段重灌")
            print("  (无输出即未发现滞后残留)")

        if args.dry_run:
            print("\n[dry-run] 未做任何改动。加 --apply 执行修复。")
            return 0

        print("\n" + "=" * 82)
        print("执行: 删除收盘整点幽灵 bar")
        print("=" * 82)
        n = delete_ghosts(session, sorted(symbols))
        print(f"  删除 {n} 根幽灵 bar")

        if args.fixlag:
            print("\n" + "=" * 82)
            print("执行: 修复滞后/不符残留 (删区间 -> 重灌)")
            print("=" * 82)
            from backend.storage.history_backfill import repair_minute_gaps
            fixed = 0
            for sym in sorted(symbols):
                start, end, nbad = find_lag_range(session, sym)
                if not nbad:
                    continue
                removed = purge_range(session, sym, start, end)
                try:
                    res = repair_minute_gaps(sym)
                    print(f"  {sym:10} 不符{nbad:>4}根 [{start} ~ {end}] "
                          f"删{removed:>4}行 补{res.get('inserted', 0):>4}根")
                    fixed += 1
                except Exception as e:
                    print(f"  {sym:10} 重灌失败: {e}")
            print(f"  共修复 {fixed} 个合约")
            session.commit()

        if misaligned:
            print("\n" + "=" * 82)
            print("执行: 清除错位夜盘整段 (待按正确口径重灌)")
            print("=" * 82)
            for sym in misaligned:
                n, days = purge_misaligned_range(session, sym)
                print(f"  {sym:10} 删除 {n:5} 行 (涉及 {days} 个错位交易日)")

            if args.rebackfill:
                print("\n" + "=" * 82)
                print("执行: 按正确口径重灌 (新浪 -1m)")
                print("=" * 82)
                from backend.storage.history_backfill import repair_minute_gaps
                for sym in misaligned:
                    try:
                        res = repair_minute_gaps(sym)
                        print(f"  {sym:10} 补 {res.get('inserted', 0)} 根  "
                              f"修正交易日 {res.get('corrected_trading_days', 0)}  "
                              f"修正字段 {res.get('corrected_fields', 0)}")
                    except Exception as e:
                        print(f"  {sym:10} 重灌失败: {e}")
            else:
                print("\n  >> 未指定 --rebackfill, 需另行重灌。")

        print("\n" + "=" * 82)
        print("重建 Redis 近端缓存")
        print("=" * 82)
        for sym in sorted(symbols):
            try:
                rebuild_redis(sym, session)
            except Exception as e:
                print(f"  {sym}: Redis 重建失败 {e}")
        print("  完成")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
