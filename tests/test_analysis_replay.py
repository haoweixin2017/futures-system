import unittest
from datetime import datetime
from decimal import Decimal

try:
    import test_bootstrap  # noqa: F401
except ImportError:
    from tests import test_bootstrap  # noqa: F401

from backend.indicator_engine.replay import evaluate_signal_outcome, summarize_outcomes
from tests.test_realtime_analyzer import bar


def signal(side="BUY"):
    return {
        "signal_id": "RB2610:5m:TEST:1", "side": side,
        "trigger_price": 100, "invalidation_price": 98 if side == "BUY" else 102,
        "target_price": 103 if side == "BUY" else 97,
    }


class AnalysisReplayTests(unittest.TestCase):
    def test_same_bar_stop_and_target_uses_conservative_stop(self):
        future = [bar("20260916", datetime(2026, 9, 16, 10), 101,
                      high=104, low=97, period="5m")]
        result = evaluate_signal_outcome(signal(), future, 10, Decimal("1"))
        self.assertEqual(result["exit_reason"], "STOP")
        self.assertEqual(result["gross_pnl_per_lot"], -20.0)

    def test_costs_and_slippage_are_deducted(self):
        future = [bar("20260916", datetime(2026, 9, 16, 10), 103,
                      high=103, low=100, period="5m")]
        result = evaluate_signal_outcome(
            signal(), future, 10, Decimal("1"),
            commission_per_lot=Decimal("5"), slippage_ticks=Decimal("1"),
        )
        self.assertEqual(result["exit_reason"], "TARGET")
        self.assertEqual(result["net_pnl_per_lot"], 5.0)

    def test_summary_reports_win_rate_and_excursions(self):
        outcomes = [
            {"net_pnl_per_lot": 10, "exit_reason": "TARGET", "r_multiple": 1.0, "mfe_points": 3, "mae_points": 1},
            {"net_pnl_per_lot": -5, "exit_reason": "STOP", "r_multiple": -0.5, "mfe_points": 1, "mae_points": 2},
        ]
        result = summarize_outcomes(outcomes)
        self.assertEqual(result["signals"], 2)
        self.assertEqual(result["win_rate_pct"], 50.0)
        self.assertEqual(result["total_net_pnl_per_lot"], 5)


if __name__ == "__main__":
    unittest.main()
