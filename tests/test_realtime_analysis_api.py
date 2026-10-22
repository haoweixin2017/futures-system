import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    import test_bootstrap  # noqa: F401
except ImportError:
    from tests import test_bootstrap  # noqa: F401

from fastapi.testclient import TestClient

from backend.api.routes import app
from backend.common.config import cfg
from backend.common.security import hash_password
from backend.storage.redis_client import RedisClient
from tests.test_realtime_analyzer import daily_bars, five_minute_bars, quote


client = TestClient(app)


class RealtimeAnalysisApiTests(unittest.TestCase):
    def setUp(self):
        client.cookies.clear()
        self.redis = RedisClient.get_client()
        self.redis.delete(
            "quote:sina:RB2610", "quote:ctp:RB2610", "quote:RB2610",
            "bars:RB2610:1d", "bars:RB2610:5m", "analysis:v1:sina:RB2610:5m",
        )
        old_feed = cfg.system.feed
        cfg.system.feed = "ctp"
        self.addCleanup(setattr, cfg.system, "feed", old_feed)
        users = {
            name: {"role": name, "display_name": name,
                   "password_hash": hash_password("test-only-strong-password")}
            for name in ("sim", "live")
        }
        users_patch = patch("backend.api.auth.load_users", return_value=users)
        users_patch.start()
        self.addCleanup(users_patch.stop)

    def login(self, role):
        return client.post("/api/v1/auth/login", json={
            "username": role, "password": "test-only-strong-password"
        })

    def seed(self):
        now = datetime.now()
        self.redis.hset("quote:sina:RB2610", mapping=quote(now))
        self.redis.hset("quote:ctp:RB2610", mapping={**quote(now), "last_price": "999"})
        self.redis.rpush("bars:RB2610:1d", *[
            json.dumps(b.to_redis_dict()) for b in daily_bars()
        ])
        self.redis.rpush("bars:RB2610:5m", *[
            json.dumps(b.to_redis_dict()) for b in five_minute_bars()
        ])

    def test_requires_login_and_sim_role(self):
        self.assertEqual(client.get("/api/v1/analysis/RB2610").status_code, 401)
        self.login("live")
        self.assertEqual(client.get("/api/v1/analysis/RB2610").status_code, 403)

    def test_uses_only_sina_data_and_writes_namespaced_cache(self):
        self.seed()
        self.login("sim")
        with patch("backend.trade_engine.paper_accounts.paper_accounts.on_signal_triggered") as bridge:
            response = client.get("/api/v1/analysis/rb2610")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["feed"], "sina")
        self.assertEqual(body["data"]["latest_price"], 104.0)
        self.assertIsNotNone(self.redis.get("analysis:v1:sina:RB2610:5m"))
        bridge.assert_not_called()

    def test_cache_is_invalidated_by_new_quote_time(self):
        self.seed()
        self.login("sim")
        first = client.get("/api/v1/analysis/RB2610").json()
        new_quote = quote(datetime.now() + timedelta(seconds=1), price=106)
        self.redis.hset("quote:sina:RB2610", mapping=new_quote)
        second = client.get("/api/v1/analysis/RB2610").json()
        self.assertNotEqual(first["data"]["quote_time"], second["data"]["quote_time"])
        self.assertEqual(second["data"]["latest_price"], 106.0)

    def test_cached_ready_result_cannot_outlive_quote_freshness(self):
        self.seed()
        self.login("sim")
        self.redis.hset("quote:sina:RB2610", mapping=quote(datetime.now() - timedelta(seconds=26)))
        cached = {"code": 0, "data": {
            "quote_time": self.redis.hget("quote:sina:RB2610", "timestamp"),
            "analysis_status": "READY", "signal": {"state": "CONFIRMED"},
        }}
        self.redis.set("analysis:v1:sina:RB2610:5m", json.dumps(cached), ex=3)
        with patch("backend.common.trading_time.FuturesTradingCalendar.is_trading_time_for", return_value=True):
            data = client.get("/api/v1/analysis/RB2610").json()["data"]
        self.assertEqual(data["analysis_status"], "STALE_DATA")
        self.assertIsNone(data["signal"])

    def test_cached_ready_result_is_not_shown_after_market_closes(self):
        self.seed()
        self.login("sim")
        cached = {"code": 0, "data": {
            "quote_time": self.redis.hget("quote:sina:RB2610", "timestamp"),
            "analysis_status": "READY", "signal": {"state": "CONFIRMED"},
        }}
        self.redis.set("analysis:v1:sina:RB2610:5m", json.dumps(cached), ex=3)
        with patch("backend.common.trading_time.FuturesTradingCalendar.is_trading_time_for", return_value=False):
            data = client.get("/api/v1/analysis/RB2610").json()["data"]
        self.assertEqual(data["analysis_status"], "MARKET_CLOSED")
        self.assertIsNone(data["signal"])


if __name__ == "__main__":
    unittest.main()
