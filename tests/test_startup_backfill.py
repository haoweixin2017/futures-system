import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from backend.storage import history_backfill

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap


def setUpModule():
    """test_bootstrap 全局屏蔽了回填入口, 本模块测真实实现, 临时还原。"""
    test_bootstrap.pause_backfill_patch()


def tearDownModule():
    test_bootstrap.resume_backfill_patch()


class StartupBackfillTests(unittest.TestCase):
    def test_full_backfill_fetches_all_periods_despite_existing_recent_data(self):
        with patch.object(history_backfill, "get_stored_bar_stats") as stats, \
                patch.object(history_backfill, "backfill_symbol", return_value={"symbol": "FU2611"}) as backfill:
            result = history_backfill.ensure_symbol_backfilled("FU2611", full_history=True)
        stats.assert_not_called()
        backfill.assert_called_once_with("FU2611")
        self.assertEqual(result, {"symbol": "FU2611"})

    def test_stale_periods_backfill_even_with_many_existing_bars(self):
        now = datetime(2026, 9, 28, 9, 30)
        stats = {period: (100, now - timedelta(minutes=10))
                 for period in history_backfill.ALL_PERIODS}
        stats["15m"] = (100, now - timedelta(days=2))
        stats["1m"] = (100, now - timedelta(minutes=1))
        stats["1d"] = (24, datetime(2026, 9, 23, 15))
        with patch.object(history_backfill, "get_stored_bar_stats", return_value=stats), \
                patch.object(history_backfill, "backfill_symbol") as backfill:
            history_backfill.ensure_symbol_backfilled("IH2610", now=now)
        backfill.assert_called_once_with("IH2610", periods=["15m", "1d"])

    def test_recent_data_does_not_trigger_backfill(self):
        now = datetime(2026, 9, 28, 9, 30)
        stats = {period: (100, now - timedelta(minutes=10))
                 for period in history_backfill.ALL_PERIODS}
        stats["1m"] = (100, now - timedelta(minutes=1))
        stats["1d"] = (24, datetime(2026, 9, 24, 15))
        with patch.object(history_backfill, "get_stored_bar_stats", return_value=stats), \
                patch.object(history_backfill, "backfill_symbol") as backfill:
            self.assertIsNone(history_backfill.ensure_symbol_backfilled("IH2610", now=now))
        backfill.assert_not_called()

    def test_daily_backfill_after_first_post_holiday_close(self):
        now = datetime(2026, 9, 28, 16)
        stats = {period: (100, now - timedelta(minutes=1))
                 for period in history_backfill.ALL_PERIODS}
        stats["1d"] = (24, datetime(2026, 9, 24, 15))
        with patch.object(history_backfill, "get_stored_bar_stats", return_value=stats), \
                patch.object(history_backfill, "backfill_symbol") as backfill:
            history_backfill.ensure_symbol_backfilled("IH2610", now=now)
        backfill.assert_called_once_with("IH2610", periods=["1d"])

    def test_backfill_discards_unfinished_high_period_bar(self):
        now = datetime.now()
        source = [{"bar_time": now - timedelta(minutes=5)},
                  {"bar_time": now + timedelta(minutes=20)}]
        with patch.object(history_backfill, "fetch_sina_minute_bars", return_value=source), \
                patch.object(history_backfill, "save_bars_to_mysql", return_value=1) as save, \
                patch.object(history_backfill, "sync_redis_if_empty"):
            history_backfill.backfill_symbol("FU2611", periods=["60m"], sleep_between=0)
        save.assert_called_once_with([source[0]])


if __name__ == "__main__":
    unittest.main()
