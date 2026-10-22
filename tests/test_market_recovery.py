import test_bootstrap
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from backend.models.market_data import BarData, TickData
from backend.bar_engine.bar_engine import MultiPeriodBarEngine
from backend.indicator_engine.indicators import IndicatorCalculator
from backend.common.trading_time import FuturesTradingCalendar


class TestMarketRecovery(unittest.TestCase):
    def make_tick(self, minute, volume, price=100):
        return TickData(instrument="RECOVERY", exchange="DCE", trading_day="20260914",
                        timestamp=datetime(2026, 9, 14, 14, minute, 30), last_price=price,
                        volume=volume, turnover=Decimal(volume * 1000), open_interest=1000)

    def setUp(self):
        self.patches = [patch("backend.bar_engine.bar_engine.db_writer.start"),
                        patch("backend.bar_engine.bar_engine.db_writer.put_bar"),
                        patch("backend.bar_engine.bar_engine.paper_accounts.on_tick"),
                        patch("backend.bar_engine.bar_engine.tape_recorder.on_tick")]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.engine = MultiPeriodBarEngine()
        self.engine.redis.hdel(self.engine.state_key, "RECOVERY")
        self.engine.generators.pop("RECOVERY", None)
        self.addCleanup(lambda: self.engine.redis.hdel(self.engine.state_key, "RECOVERY"))

    def test_restarts_restore_forming_volume_and_ignore_replay(self):
        engine = self.engine
        engine.on_tick(self.make_tick(58, 100))
        engine.on_tick(self.make_tick(59, 110))
        restored = MultiPeriodBarEngine()
        self.assertEqual(restored.generators["RECOVERY"].current_bar.volume, 10)
        replay = self.make_tick(59, 110)
        restored.on_tick(replay)
        self.assertEqual(restored.generators["RECOVERY"].current_bar.volume, 10)
        self.assertEqual(restored.synthetic_bars["RECOVERY"]["15m"].bar_time, engine.synthetic_bars["RECOVERY"]["15m"].bar_time)

    def test_close_without_new_tick_and_no_duplicate_daily_bar(self):
        engine = self.engine
        engine.on_tick(self.make_tick(58, 100))
        engine.on_tick(self.make_tick(59, 110))
        engine.close_sessions(datetime(2026, 9, 14, 15))
        self.assertIsNone(engine.generators["RECOVERY"].current_bar)
        self.assertIsNone(engine.daily_bars["RECOVERY"])
        count = len(engine.history_bars["RECOVERY"]["1d"])
        engine.close_sessions(datetime(2026, 9, 14, 15, 1))
        self.assertEqual(len(engine.history_bars["RECOVERY"]["1d"]), count)
        late = self.make_tick(59, 111).model_copy(update={"timestamp": datetime(2026, 9, 14, 15)})
        engine.on_tick(late)
        engine.close_sessions(datetime(2026, 9, 14, 15, 1))
        self.assertEqual(len(engine.history_bars["RECOVERY"]["1d"]), count)

    def test_missing_turnover_is_unavailable_and_vwap_uses_one_day(self):
        bar = BarData(instrument="JM2701", period="1m", trading_day="20260914",
                      bar_time=datetime(2026, 9, 14, 9), open_price=100, close_price=100,
                      high_price=100, low_price=100, volume=10, turnover=0, open_interest=1000)
        self.assertIsNone(IndicatorCalculator.calculate_vwap([bar], 60))
        bar.turnover = Decimal(60000)
        previous = bar.model_copy(update={"trading_day": "20260911", "turnover": Decimal(120000)})
        indicators = IndicatorCalculator.compute_all_indicators([previous, bar], volume_multiple=60)
        self.assertEqual(indicators["vwap"], "100.00")

    def test_friday_overnight_stays_on_monday(self):
        self.assertEqual(FuturesTradingCalendar.get_trading_day(datetime(2026, 9, 12, 1)), "20260914")
        self.assertEqual(FuturesTradingCalendar.session_end(datetime(2026, 9, 11, 23, 30), "SC2610"), datetime(2026, 9, 12, 2, 30))
