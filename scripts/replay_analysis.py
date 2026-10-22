"""量价分析逐根回放；只读数据库，不订阅行情、不写订单。

示例：
  .venv\Scripts\python.exe scripts\replay_analysis.py --instrument SC2611 --start 20260801 --end 20260916
"""

import argparse
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.common.product_specs import get_spec
from backend.indicator_engine.indicators import IndicatorCalculator
from backend.indicator_engine.realtime_analyzer import RealtimeAnalyzer
from backend.indicator_engine.replay import evaluate_signal_outcome, summarize_outcomes
from backend.models.database import MarketBarModel
from backend.models.market_data import BarData
from backend.storage.mysql_client import SessionLocal


def as_bar(row):
    return BarData(
        instrument=row.instrument_id, trading_day=row.trading_day, period=row.period,
        bar_time=row.bar_time, open_price=row.open_price, high_price=row.high_price,
        low_price=row.low_price, close_price=row.close_price, volume=row.volume,
        turnover=row.turnover, open_interest=row.open_interest,
        open_interest_change=row.open_interest_change,
    )


def load_bars(instrument, period):
    session = SessionLocal()
    try:
        rows = session.query(MarketBarModel).filter_by(
            instrument_id=instrument, period=period
        ).order_by(MarketBarModel.bar_time.asc()).all()
        return [as_bar(row) for row in rows]
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="模拟盘量价信号逐根回放")
    parser.add_argument("--instrument", required=True, help="具体交割合约，例如 SC2611")
    parser.add_argument("--start", help="起始交易日 YYYYMMDD")
    parser.add_argument("--end", help="结束交易日 YYYYMMDD")
    parser.add_argument("--horizon", type=int, default=12, help="确认后最多评估多少根5m K")
    parser.add_argument("--commission-per-lot", type=Decimal, default=Decimal("0"))
    parser.add_argument("--slippage-ticks", type=Decimal, default=Decimal("0"))
    parser.add_argument("--summary-only", action="store_true", help="只输出汇总，不输出逐笔明细")
    args = parser.parse_args()

    instrument = args.instrument.strip().upper()
    daily = load_bars(instrument, "1d")
    bars = load_bars(instrument, "5m")
    volume_multiple, tick_value = get_spec(instrument)
    tick = Decimal(str(tick_value))
    outcomes = []
    seen = set()

    for index, current in enumerate(bars):
        if args.start and current.trading_day < args.start:
            continue
        if args.end and current.trading_day > args.end:
            continue
        prefix = bars[:index + 1]
        day_prefix = [item for item in prefix if item.trading_day == current.trading_day]
        daily_prefix = [item for item in daily if item.trading_day < current.trading_day][-60:]
        if len(daily_prefix) < 25 or len(day_prefix) < 6:
            continue
        vwap = IndicatorCalculator.calculate_vwap(day_prefix, volume_multiple)
        quote_time = current.bar_time + timedelta(minutes=5)
        quote = {
            "instrument": instrument,
            "trading_day": current.trading_day,
            "timestamp": quote_time.isoformat(),
            "last_price": str(current.close_price),
            "day_open": str(day_prefix[0].open_price),
            "day_high": str(max(item.high_price for item in day_prefix)),
            "day_low": str(min(item.low_price for item in day_prefix)),
            "avg_price": str(vwap) if vwap is not None else "",
            "volume": str(sum(item.volume for item in day_prefix)),
            "open_interest": str(current.open_interest),
            # 历史 Bar 没有盘口；只为通过流动性门禁构造一跳价差，结果中明确标注该事实。
            "bid_price_1": str(current.close_price - tick),
            "ask_price_1": str(current.close_price + tick),
        }
        analysis = RealtimeAnalyzer.analyze(
            instrument=instrument, instrument_name=instrument, quote=quote,
            daily_bars=daily_prefix, bars_5m=prefix[-2500:],
            volume_multiple=volume_multiple, price_tick=tick, now=quote_time,
        )
        signal = analysis.get("signal")
        if (not signal or signal.get("state") != "CONFIRMED"
                or not signal.get("actionable") or signal["signal_id"] in seen):
            continue
        seen.add(signal["signal_id"])
        future = bars[index + 1:index + 1 + max(1, args.horizon)]
        outcomes.append(evaluate_signal_outcome(
            signal, future, volume_multiple, tick,
            args.commission_per_lot, args.slippage_ticks,
        ))

    output = {
        "instrument": instrument,
        "start": args.start,
        "end": args.end,
        "horizon_5m_bars": args.horizon,
        "orderbook_assumption": "历史无盘口，使用收盘价上下各一跳，仅用于通过流动性门禁",
        "commission_per_lot": float(args.commission_per_lot),
        "slippage_ticks_each_side": float(args.slippage_ticks),
        "summary": summarize_outcomes(outcomes),
    }
    if not args.summary_only:
        output["outcomes"] = outcomes
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
