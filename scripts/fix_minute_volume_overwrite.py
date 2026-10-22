"""修复 1m K 线 volume 存量脏数据 (被新浪分钟线源覆写污染)。

背景
----
`repair_minute_gaps` 原先会用新浪 `getFewMinLine` 的 `v` 字段覆写本地 1m volume。
新浪该字段分布严重扭曲 (按秒去重统计, 同秒多笔被合并、相邻分钟又重复计数),
实测同一交易日 21:40=44 / 21:44=38 / 21:49=89, 而同期真实成交量是几千手。
结果「已闭合根」的正确实时聚合值被改小 (实测 21:55 由 2238 → 190, 砍 91.5%),
再经 `_rebuild_minute_redis_cache` 回灌 Redis, 使 VOL 副图只捕获约 16%。

覆写缺陷已修复 (volume 只在「非当前交易日」才按源端纠偏), 但**不追溯修正存量**。

真值锚点
--------
5m / 15m / 30m 未受污染 (走 `_update_higher_period` 从内存对象累加,
且 `_rebuild_minute_redis_cache` 只重建 1m 键)。实测三者严格自洽:

    15m 各根 == 其内 3 个 5m 之和         (5/5 根 100.0%)
    30m 各根 == 其内 6 个 5m 之和         (2/2 根 100.0%)

**安全边界 (务必遵守, 否则会破坏正常历史数据)**
------------------------------------------------
1. **只处理 --trading-days 指定的交易日, 且默认只处理"当前交易日"**。
   实测历史交易日的 1m 构成复杂 (实时聚合 + 回补修正的混合产物), 其合计本就
   可能高于 5m 锚点 (09-11 为 131%, 09-14 为 120%), **盲目重算会破坏正确数据**。
2. **只补不削**: 仅当 5m 区间内「1m 之和 < 锚点值」且差额超阈值时才动手。
   1m 之和 ≥ 锚点的区间一律跳过 (那些多半是正常的)。
3. 增量按 1m 原有**分布形态**等比摊回 —— 保留相对活跃度, 且严格保证
   「修复后该 5m 区间内 1m 之和 == 锚点值」。

用法
----
    # 体检 (默认, 不改动)
    python scripts/fix_minute_volume_overwrite.py --dry-run

    # 实际修复当前交易日并重建 Redis
    python scripts/fix_minute_volume_overwrite.py --apply --rebuild-redis

    # 指定交易日 / 合约
    python scripts/fix_minute_volume_overwrite.py --apply --trading-days 20260915
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.common.trading_time import FuturesTradingCalendar
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel
from backend.storage.redis_client import RedisClient

ANCHOR_PERIOD = "5m"
ANCHOR_MINUTES = 5
# 差额阈值: 相对锚点 5% 且绝对 50 手以上才算"偏低"
REL_THRESHOLD = 0.05
ABS_THRESHOLD = 50


def _anchor_start(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // ANCHOR_MINUTES) * ANCHOR_MINUTES,
                      second=0, microsecond=0)


def load_anchors(client, symbol: str, trading_day: str) -> Dict[datetime, int]:
    out: Dict[datetime, int] = {}
    for raw in client.lrange(f"bars:{symbol}:{ANCHOR_PERIOD}", -1000, -1):
        try:
            b = json.loads(raw)
        except Exception:
            continue
        if b.get("trading_day") != trading_day:
            continue
        try:
            out[datetime.fromisoformat(b["bar_time"])] = int(b.get("volume") or 0)
        except Exception:
            continue
    return out


def plan_fix(rows: List[MarketBarModel], anchors: Dict[datetime, int]):
    """返回 (changes, skipped)。changes: [(row, old, new)]"""
    groups: Dict[datetime, List[MarketBarModel]] = defaultdict(list)
    for r in rows:
        groups[_anchor_start(r.bar_time)].append(r)

    changes: List[Tuple[MarketBarModel, int, int]] = []
    skipped: List[str] = []

    for start in sorted(groups):
        members = sorted(groups[start], key=lambda x: x.bar_time)
        anchor = anchors.get(start)
        if anchor is None:
            skipped.append(start.strftime("%H:%M") + "无锚点")
            continue

        olds = [int(r.volume or 0) for r in members]
        cur = sum(olds)
        gap = anchor - cur
        # 只补不削
        if gap <= max(ABS_THRESHOLD, anchor * REL_THRESHOLD):
            continue
        if cur <= 0:
            # 原值全为 0: 无分布可依, 按根数均分
            news = [gap // len(olds)] * len(olds)
            news[-1] += gap - sum(news)
        else:
            news, allocated = [], 0
            for i, ov in enumerate(olds):
                if i == len(olds) - 1:
                    news.append(gap - allocated)
                else:
                    sh = int(round(gap * ov / cur))
                    news.append(sh)
                    allocated += sh
            if any(n < 0 for n in news):
                news = [int(gap * ov // cur) for ov in olds]
                news[-1] += gap - sum(news)

        for r, ov, add in zip(members, olds, news):
            if add:
                changes.append((r, ov, ov + add))

    return changes, skipped


def rebuild_redis(client, symbol: str, max_keep: int = 500) -> None:
    """用 MySQL 现值重建 Redis bars:{symbol}:1m (保留近 2 分钟实时属地)。"""
    key = f"bars:{symbol}:1m"
    cutoff = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=2)

    keep = {}
    for raw in client.lrange(key, -max_keep, -1):
        try:
            d = json.loads(raw)
            bt = datetime.fromisoformat(d["bar_time"])
        except Exception:
            continue
        if bt >= cutoff:
            keep[bt.strftime("%Y-%m-%dT%H:%M:00")] = raw

    session = SessionLocal()
    try:
        rows = (session.query(MarketBarModel).filter_by(instrument_id=symbol, period="1m")
                .order_by(MarketBarModel.bar_time.desc()).limit(max_keep).all())
    finally:
        session.close()

    by_key = dict(keep)
    for r in rows:
        if r.bar_time >= cutoff:
            continue
        by_key[r.bar_time.strftime("%Y-%m-%dT%H:%M:00")] = json.dumps({
            "instrument": r.instrument_id,
            "trading_day": r.trading_day,
            "period": "1m",
            "bar_time": r.bar_time.isoformat(),
            "open_price": str(r.open_price),
            "high_price": str(r.high_price),
            "low_price": str(r.low_price),
            "close_price": str(r.close_price),
            "volume": int(r.volume or 0),
            "turnover": str(r.turnover),
            "open_interest": str(r.open_interest),
            "open_interest_change": str(r.open_interest_change),
        })

    ordered = [by_key[t] for t in sorted(by_key)[-max_keep:]]
    pipe = client.pipeline(transaction=False)
    pipe.delete(key)
    if ordered:
        pipe.rpush(key, *ordered)
    pipe.execute()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="修复 1m volume 存量脏数据 (被新浪分钟线源覆写)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="实际写库")
    ap.add_argument("--dry-run", action="store_true", help="只体检 (默认)")
    ap.add_argument("--symbols", default="FU2611")
    ap.add_argument("--trading-days", default="",
                    help="逗号分隔 YYYYMMDD; 留空=仅当前交易日 (安全默认)")
    ap.add_argument("--all-days", action="store_true",
                    help="危险: 处理库中全部交易日 (历史日构成复杂, 慎用)")
    ap.add_argument("--rebuild-redis", action="store_true")
    args = ap.parse_args()

    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    now = datetime.now()
    cur_td = FuturesTradingCalendar.get_trading_day(now)

    if args.trading_days:
        days: Optional[set] = {x.strip() for x in args.trading_days.split(",") if x.strip()}
    elif args.all_days:
        days = None
    else:
        days = {cur_td}

    client = RedisClient.get_client()
    session = SessionLocal()
    total = 0
    try:
        for sym in symbols:
            print("=" * 76)
            print(f"合约 {sym}   目标交易日: {sorted(days) if days else '全部(危险)'}"
                  f"   当前交易日: {cur_td}")
            print("=" * 76)

            q = (session.query(MarketBarModel)
                 .filter_by(instrument_id=sym, period="1m")
                 .order_by(MarketBarModel.bar_time).all())
            if not q:
                print("  库中无 1m 数据, 跳过")
                continue

            by_day = defaultdict(list)
            for r in q:
                if days is None or r.trading_day in days:
                    by_day[r.trading_day].append(r)

            for td in sorted(by_day):
                rows = by_day[td]
                anchors = load_anchors(client, sym, td)
                if not anchors:
                    print(f"  [{td}] 无 {ANCHOR_PERIOD} 锚点, 跳过")
                    continue
                changes, skipped = plan_fix(rows, anchors)
                print(f"  [{td}] 1m {len(rows):>4} 根 | 锚点 {len(anchors):>3} 个 | "
                      f"待补 {len(changes):>4} 根")
                if skipped:
                    print(f"         无锚点区间: {', '.join(skipped)}")
                shown = 0
                for r, ov, nv in changes:
                    if shown < 6:
                        print(f"           {r.bar_time.strftime('%H:%M')}  {ov:>6} -> {nv:>6}  "
                              f"(+{nv - ov})")
                    shown += 1
                if len(changes) > 6:
                    print(f"           ... 另有 {len(changes) - 6} 根")
                if args.apply:
                    for r, ov, nv in changes:
                        r.volume = nv
                    total += len(changes)

            if args.apply:
                session.commit()
                print(f"  >> 已提交 {sym}")
                if args.rebuild_redis:
                    rebuild_redis(client, sym)
                    print(f"  >> Redis bars:{sym}:1m 已重建")

        print()
        print("=" * 76)
        if args.apply:
            print(f"完成: 共修正 {total} 根 1m K线")
        else:
            print("体检完成 (未改动)。实际执行: --apply --rebuild-redis")
        print("=" * 76)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
