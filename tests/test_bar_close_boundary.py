"""CTP/新浪收盘快照边界回归测试。

历史故障: 交易所收盘静态快照 (15:00:00.xxx / 23:00:00.xxx) 因 bar_engine 用严格相等
判断收盘时刻而漏判, 被当作新分钟开出一根 OHLCV/OI 与前一分钟完全相同的"幽灵 bar"。
全库 1m 曾积累 137 根此类脏数据 (15:00 有 98 根 / 23:00 有 39 根)。
"""
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models.market_data import TickData
from backend.bar_engine.bar_engine import BarGenerator

SYM = "FU2611"
DAY = "20260914"


def mk(ts: datetime, price=4000, vol=100, oi=500) -> TickData:
    return TickData(
        instrument=SYM, exchange="SHFE", trading_day=DAY, timestamp=ts,
        last_price=Decimal(str(price)), volume=vol, turnover=Decimal(str(price * vol)),
        open_interest=Decimal(str(oi)),
        bid_price_1=Decimal(str(price)), ask_price_1=Decimal(str(price)),
        bid_volume_1=1, ask_volume_1=1,
    )


class TestCloseSnapshotBoundary(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.g = BarGenerator(SYM, lambda b: self.closed.append(b))

    def _feed(self, *ticks):
        for t in ticks:
            self.g.update_tick(t)
        return self.g.current_bar

    def test_close_snapshot_with_microseconds_does_not_open_ghost_bar(self):
        """15:00:00.500 收盘快照必须并入 14:59, 不得新开 15:00 bar。"""
        bar = self._feed(
            mk(datetime(2026, 9, 14, 14, 58, 0), 3150, 100, 500),
            mk(datetime(2026, 9, 14, 14, 59, 30), 3152, 110, 505),
            mk(datetime(2026, 9, 14, 15, 0, 0, 500000), 3152, 110, 505),
        )
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 14, 59))

    def test_close_snapshot_one_second_late_does_not_open_ghost_bar(self):
        """15:00:01 迟到快照同样不得新开 bar。"""
        bar = self._feed(
            mk(datetime(2026, 9, 14, 14, 59, 10), 3150, 100, 500),
            mk(datetime(2026, 9, 14, 15, 0, 1), 3155, 120, 510),
        )
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 14, 59))

    def test_night_close_snapshot_23_00(self):
        """夜盘 23:00:00.300 收盘快照并入 22:59。"""
        bar = self._feed(
            mk(datetime(2026, 9, 14, 22, 59, 10), 4300, 200, 600),
            mk(datetime(2026, 9, 14, 23, 0, 0, 300000), 4302, 205, 601),
        )
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 22, 59))

    def test_cold_start_with_close_snapshot(self):
        """冷启动 (current_bar is None) 直接收到收盘快照, 也必须落在 14:59。"""
        bar = self._feed(mk(datetime(2026, 9, 14, 15, 0, 0, 800000), 4289, 500, 700))
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 14, 59))

    def test_break_snapshot_is_ignored(self):
        """10:15:00.200 属上午小休静默快照, 不得让 bar_time 前进到 10:15。"""
        bar = self._feed(
            mk(datetime(2026, 9, 14, 10, 14, 50), 4280, 10, 100),
            mk(datetime(2026, 9, 14, 10, 15, 0, 200000), 4280, 12, 101),
        )
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 10, 14))

    def test_normal_intraday_ticks_unaffected(self):
        """常规盘中 tick 的分钟归属不受边界修复影响。"""
        bar = self._feed(mk(datetime(2026, 9, 14, 14, 30, 5), 4280, 10, 100))
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 14, 30))
        bar = self._feed(mk(datetime(2026, 9, 14, 14, 31, 10), 4285, 20, 105))
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 14, 31))

    def test_night_session_open_not_shifted(self):
        """夜盘开盘 21:00:00 是真实交易分钟, 不能被误平移成 20:59。"""
        bar = self._feed(mk(datetime(2026, 9, 14, 21, 0, 0), 4300, 5, 200))
        self.assertEqual(bar.bar_time, datetime(2026, 9, 14, 21, 0))

    def test_first_night_tick_counts_opening_volume(self):
        bar = self._feed(mk(datetime(2026, 9, 14, 21, 0, 8), vol=120))
        self.assertEqual(bar.volume, 120)
        bar = self._feed(mk(datetime(2026, 9, 14, 21, 0, 35), vol=150))
        self.assertEqual(bar.volume, 150)

    def test_first_day_tick_after_no_night_counts_opening_volume(self):
        # 国庆前假期后 9/28 交易日没有前一交易日晚盘。
        bar = self._feed(mk(datetime(2026, 9, 28, 9, 0, 8), vol=45000))
        self.assertEqual(bar.volume, 45000)

    def test_day_open_after_night_keeps_cumulative_baseline(self):
        self._feed(mk(datetime(2026, 9, 14, 22, 59, 10), vol=10000))
        self.g.current_bar = None  # 夜盘收盘后 close_sessions 的状态
        bar = self._feed(mk(datetime(2026, 9, 15, 9, 0, 8), vol=11000))
        self.assertEqual(bar.volume, 1000)

    def test_new_night_open_after_day_bar_counts_from_zero(self):
        self._feed(mk(datetime(2026, 9, 14, 14, 59, 10), vol=10000))
        bar = self._feed(mk(datetime(2026, 9, 14, 21, 0, 8), vol=120))
        self.assertEqual(bar.volume, 120)

    def test_late_first_tick_does_not_assign_prior_volume_to_current_minute(self):
        bar = self._feed(mk(datetime(2026, 9, 28, 9, 2, 8), vol=45000))
        self.assertEqual(bar.volume, 0)
        bar = self._feed(mk(datetime(2026, 9, 28, 9, 2, 35), vol=45200))
        self.assertEqual(bar.volume, 200)

    def test_close_snapshot_zero_volume_after_last_bar(self):
        """先闭合 14:59 再收到收盘快照, 收盘快照不得产生新 bar。"""
        self._feed(
            mk(datetime(2026, 9, 14, 14, 59, 20), 4280, 100, 100),
            mk(datetime(2026, 9, 14, 15, 0, 30), 4289, 100, 100),
        )
        times = [b.bar_time for b in self.closed]
        self.assertNotIn(datetime(2026, 9, 14, 15, 0), times)
        self.assertNotIn(datetime(2026, 9, 14, 15, 0), [self.g.current_bar.bar_time])


class TestCloseSessionsGuard(unittest.TestCase):
    """close_sessions 的兜底防线: 收盘整点残影 bar 必须被丢弃而非出库。"""

    def test_boundary_bar_is_dropped(self):
        from backend.bar_engine.bar_engine import MultiPeriodBarEngine
        from backend.models.market_data import BarData

        engine = MultiPeriodBarEngine.__new__(MultiPeriodBarEngine)
        import threading
        engine._lock = threading.RLock()
        engine.generators = {}
        engine.synthetic_bars = {}
        engine.daily_bars = {}
        engine.history_bars = {}
        engine.generators[SYM] = BarGenerator(SYM, lambda b: None)
        engine.synthetic_bars[SYM] = {p: None for p in ["5m", "15m", "30m", "60m"]}
        engine.daily_bars[SYM] = None
        engine.history_bars[SYM] = {}

        emitted = []
        engine.on_1m_bar_close = lambda b: emitted.append(b)
        engine.save_forming = lambda inst: None
        engine._save_bar_to_redis = lambda b: None

        ghost = BarData(
            instrument=SYM, trading_day=DAY, period="1m",
            bar_time=datetime(2026, 9, 14, 15, 0),
            open_price=Decimal("100"), high_price=Decimal("100"),
            low_price=Decimal("100"), close_price=Decimal("100"),
            volume=0, turnover=Decimal("0"), open_interest=Decimal("1"),
            open_interest_change=Decimal("0"),
        )
        engine.generators[SYM].current_bar = ghost
        engine.close_sessions(now=datetime(2026, 9, 14, 15, 0, 30))

        self.assertEqual(emitted, [], "收盘整点残影 bar 不应出库")
        self.assertIsNone(engine.generators[SYM].current_bar)


if __name__ == "__main__":
    unittest.main(verbosity=2)
