import test_bootstrap
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.api.routes import app
from backend.bar_engine.live_bars import forming_bar, merge_forming


class TestLiveBars(unittest.TestCase):
    def setUp(self):
        clock = patch("backend.bar_engine.live_bars.datetime", wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = datetime(2026, 9, 14, 9, 10)
        self.minute = dict(instrument="BU2611", period="1m", trading_day="20260914",
                           bar_time="2026-09-14T09:09:00", open_price="101", high_price="104",
                           low_price="100", close_price="103", volume=7, turnover="7210",
                           open_interest="200", open_interest_change="2")
        self.base = dict(self.minute, period="15m", bar_time="2026-09-14T09:00:00",
                         open_price="99", high_price="102", low_price="98", close_price="101", volume=20)
        self.state = {"minute": self.minute, "higher": {"15m": self.base},
                      "daily": dict(self.base, period="1d", bar_time="2026-09-14T15:00:00")}
        self.redis = SimpleNamespace(hget=lambda *args: json.dumps(self.state), lrange=lambda *args: [], hgetall=lambda *args: {})

    def test_current_minute_and_daily_update_before_close(self):
        for period in ("1m", "15m", "1d"):
            result = forming_bar(self.redis, "sina", "BU2611", period)
            self.assertEqual(result["close_price"], "103")
            self.assertFalse(result["is_closed"])
        self.minute["close_price"] = "104"
        self.assertEqual(forming_bar(self.redis, "sina", "BU2611", "15m")["close_price"], "104")

    def test_minute_combined_once_and_new_bucket_not_mixed(self):
        result = forming_bar(self.redis, "sina", "BU2611", "15m")
        self.assertEqual(result["volume"], 27)
        self.assertEqual(result["open_price"], "99")
        self.assertEqual(result["high_price"], "104")
        self.assertEqual(result["low_price"], "98")
        self.assertEqual(forming_bar(self.redis, "sina", "BU2611", "15m")["volume"], 27)
        self.minute["bar_time"] = "2026-09-14T09:15:00"
        result = forming_bar(self.redis, "sina", "BU2611", "15m")
        self.assertEqual(result["volume"], 7)
        self.assertEqual(result["bar_time"], "2026-09-14T09:15:00")

    def test_history_filter_and_deduplication(self):
        current = forming_bar(self.redis, "sina", "BU2611", "15m")
        self.assertEqual(len(merge_forming([self.base], current)), 1)
        self.assertEqual(merge_forming([], current, date="20260911"), [])

    def test_api_includes_forming_bars_with_and_without_date(self):
        with patch("backend.api.routes.redis_client", self.redis):
            client = TestClient(app)
            for suffix in ("", "&date=20260914"):
                result = client.get("/api/v1/bars/BU2611?period=15m" + suffix)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["bars"][-1]["close_price"], "103")
            self.assertEqual(client.get("/api/v1/bars/BU2611?period=15m&date=20260911").json()["bars"], [])

    def test_daily_uses_full_session_quote_after_late_subscription(self):
        quote = dict(trading_day="20260914", day_open="740", day_high="803.1", day_low="737.9",
                     last_price="791.8", volume="93009", turnover="0", open_interest="33895")
        self.redis.hgetall = lambda *args: quote
        bar = forming_bar(self.redis, "sina", "SC2611", "1d")
        self.assertEqual((bar["open_price"], bar["high_price"], bar["low_price"], bar["volume"]),
                         ("740", "803.1", "737.9", 93009))
        self.assertEqual(bar["bar_time"], "2026-09-14T15:00:00")
        self.assertEqual(merge_forming([], bar, date="20260911"), [])

    def test_closed_sina_daily_quote_cannot_override_history(self):
        self.redis.hgetall = lambda *args: dict(
            trading_day="20260914", day_open="101", day_high="104", day_low="100",
            last_price="103", volume="27", turnover="0", open_interest="200")
        for now in (datetime(2026, 9, 14, 15), datetime(2026, 10, 6, 12)):
            self.clock.now.return_value = now
            current = forming_bar(self.redis, "sina", "BU2611", "1d")
            self.assertIsNone(current)
            history = dict(self.state["daily"], volume=30, open_interest="201")
            self.assertEqual(merge_forming([history], current), [history])

    def test_closed_sina_daily_checkpoint_is_not_forming(self):
        self.clock.now.return_value = datetime(2026, 10, 6, 12)
        self.assertIsNone(forming_bar(self.redis, "sina", "BU2611", "1d"))

    def test_next_trading_day_daily_quote_is_still_forming(self):
        self.clock.now.return_value = datetime(2026, 9, 13, 21)
        self.assertIsNotNone(forming_bar(self.redis, "sina", "BU2611", "1d"))
