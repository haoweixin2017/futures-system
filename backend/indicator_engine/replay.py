from decimal import Decimal
from typing import Any, Dict, List

from backend.models.market_data import BarData


def evaluate_signal_outcome(
    signal: Dict[str, Any],
    future_bars: List[BarData],
    volume_multiple: int,
    price_tick: Decimal,
    commission_per_lot: Decimal = Decimal("0"),
    slippage_ticks: Decimal = Decimal("0"),
) -> Dict[str, Any]:
    """用信号后的 K 线评估一手结果；同根同时触及止损和目标时按止损先成交。"""
    entry = Decimal(str(signal["trigger_price"]))
    stop = Decimal(str(signal["invalidation_price"]))
    target = Decimal(str(signal["target_price"]))
    side = signal["side"]
    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    risk_points = abs(entry - stop)
    best = Decimal("0")
    worst = Decimal("0")
    exit_price = entry
    exit_reason = "NO_FUTURE_BARS"
    bars_held = 0

    for bars_held, bar in enumerate(future_bars, start=1):
        favorable = (bar.high_price - entry) if side == "BUY" else (entry - bar.low_price)
        adverse = (entry - bar.low_price) if side == "BUY" else (bar.high_price - entry)
        best = max(best, favorable)
        worst = max(worst, adverse)
        stop_hit = bar.low_price <= stop if side == "BUY" else bar.high_price >= stop
        target_hit = bar.high_price >= target if side == "BUY" else bar.low_price <= target
        if stop_hit:
            exit_price = stop
            exit_reason = "STOP"
            break
        if target_hit:
            exit_price = target
            exit_reason = "TARGET"
            break
        exit_price = bar.close_price
        exit_reason = "HORIZON"

    multiplier = Decimal(str(volume_multiple))
    gross_pnl = (exit_price - entry) * direction * multiplier
    friction = commission_per_lot + slippage_ticks * price_tick * multiplier * Decimal("2")
    net_pnl = gross_pnl - friction
    risk_amount = risk_points * multiplier
    return {
        "signal_id": signal["signal_id"],
        "side": side,
        "entry": float(entry),
        "exit": float(exit_price),
        "exit_reason": exit_reason,
        "bars_held": bars_held,
        "gross_pnl_per_lot": float(gross_pnl.quantize(Decimal("0.01"))),
        "net_pnl_per_lot": float(net_pnl.quantize(Decimal("0.01"))),
        "r_multiple": float((net_pnl / risk_amount).quantize(Decimal("0.01"))) if risk_amount > 0 else None,
        "mfe_points": float(best),
        "mae_points": float(worst),
    }


def summarize_outcomes(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not outcomes:
        return {
            "signals": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
            "target_hits": 0, "stop_hits": 0, "total_net_pnl_per_lot": 0.0,
            "average_r_multiple": None, "average_mfe_points": None,
            "average_mae_points": None,
        }
    wins = sum(item["net_pnl_per_lot"] > 0 for item in outcomes)
    valid_r = [item["r_multiple"] for item in outcomes if item["r_multiple"] is not None]
    count = len(outcomes)
    return {
        "signals": count,
        "wins": wins,
        "losses": count - wins,
        "win_rate_pct": round(wins / count * 100, 2),
        "target_hits": sum(item["exit_reason"] == "TARGET" for item in outcomes),
        "stop_hits": sum(item["exit_reason"] == "STOP" for item in outcomes),
        "total_net_pnl_per_lot": round(sum(item["net_pnl_per_lot"] for item in outcomes), 2),
        "average_r_multiple": round(sum(valid_r) / len(valid_r), 2) if valid_r else None,
        "average_mfe_points": round(sum(item["mfe_points"] for item in outcomes) / count, 2),
        "average_mae_points": round(sum(item["mae_points"] for item in outcomes) / count, 2),
    }
