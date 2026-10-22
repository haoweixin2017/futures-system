try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from backend.models.market_data import BarData
from backend.signal_engine.signal_engine import SignalEngine

class TestSignalEngine(unittest.TestCase):
    def setUp(self):
        self.engine = SignalEngine()

    def test_short_watch_trigger(self):
        base_time = datetime(2026, 9, 11, 9, 0, 0)
        bars_15m = []
        # 前 20 根：价格在 3200 ~ 3250 盘整，均量 1000
        for i in range(20):
            bars_15m.append(BarData(
                instrument="FU2610",
                trading_day="20260911",
                period="15m",
                bar_time=base_time + timedelta(minutes=15 * i),
                open_price=Decimal("3220.0"),
                high_price=Decimal("3250.0"),
                low_price=Decimal("3200.0"),
                close_price=Decimal("3210.0"),
                volume=1000,
                turnover=Decimal("32100000.0"),
                open_interest=Decimal("200000.0")
            ))

        # 第 21 根：向下跌破 3200 至 3180，放量至 2500，增仓 5000
        bars_15m.append(BarData(
            instrument="FU2610",
            trading_day="20260911",
            period="15m",
            bar_time=base_time + timedelta(minutes=15 * 20),
            open_price=Decimal("3200.0"),
            high_price=Decimal("3205.0"),
            low_price=Decimal("3178.0"),
            close_price=Decimal("3180.0"),
            volume=2500, # 2.5倍放量
            turnover=Decimal("79500000.0"),
            open_interest=Decimal("205000.0") # 增仓 5000
        ))

        sig = self.engine.evaluate_15m_signals("FU2610", bars_15m, [])
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal, "SHORT_WATCH")
        self.assertGreaterEqual(sig.rule_score, 85)
        print("SHORT_WATCH triggered successfully! Score:", sig.rule_score, "Invalid Price:", sig.invalid_price)


    def test_flat_market_no_crash(self):
        base_time = datetime(2026, 9, 11, 9, 0, 0)
        bars_15m = [
            BarData(
                instrument="JM2701",
                trading_day="20260911",
                period="15m",
                bar_time=base_time + timedelta(minutes=15 * i),
                open_price=Decimal("1200.0"),
                high_price=Decimal("1205.0"),
                low_price=Decimal("1195.0"),
                close_price=Decimal("1200.0"),
                volume=1000,
                turnover=Decimal("600000.0"),
                open_interest=Decimal("50000.0"),
                open_interest_change=Decimal("0.0")
            )
            for i in range(25)
        ]
        sig = self.engine.evaluate_15m_signals("JM2701", bars_15m, [])
        self.assertIsNone(sig)

if __name__ == "__main__":
    unittest.main()
