import test_bootstrap
import copy
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from pydantic import ValidationError
from backend.trade_engine.models import OrderRequest
from backend.models.market_data import TickData
from backend.trade_engine.paper_trader import PaperTrader
from backend.trade_engine.paper_accounts import PaperAccounts
from backend.trade_engine.state_store import TradingStateStore
from backend.trade_engine.conditional_models import StopOrderRequest


def tick(price=100, day="20260914", second=0):
    return TickData(instrument="JM2701", exchange="DCE", trading_day=day,
                    timestamp=datetime(2026, 9, 14, 9) + timedelta(seconds=second),
                    last_price=price, volume=100 + second, turnover=10000,
                    open_interest=1000)


def order(**kwargs):
    return OrderRequest(**dict(dict(instrument="JM2701", direction="BUY", offset="OPEN", price=100, volume=1), **kwargs))


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.fail = False

    def load(self, key):
        return copy.deepcopy(self.data.get(key, (None, 0)))

    def save(self, key, state, version):
        if self.fail:
            raise RuntimeError("storage unavailable")
        if self.data.get(key, (None, 0))[1] != version:
            raise RuntimeError("stale writer")
        self.data[key] = copy.deepcopy((state, version + 1))
        return version + 1

    def keys(self, prefix):
        return [k for k in self.data if k.startswith(prefix)]


class TestTradingSafety(unittest.TestCase):
    def setUp(self):
        self.notify_patch = patch("backend.trade_engine.paper_trader.notifier.notify")
        self.notify = self.notify_patch.start()
        self.addCleanup(self.notify_patch.stop)

    def test_invalid_order_price_rejected(self):
        for price in [-1, 0, "NaN", "Infinity"]:
            with self.assertRaises(ValidationError):
                order(price=price)

    def test_pending_orders_count_towards_position_cap(self):
        trader = PaperTrader()
        for _ in range(4):
            self.assertEqual(trader.submit_order(order(volume=50)).status, "SUBMITTED")
        self.assertEqual(trader.submit_order(order(volume=50)).status, "REJECTED")
        trader.on_tick(tick())
        self.assertEqual(sum(p.volume for p in trader.get_positions()), 200)

    def test_floating_loss_reduces_available_and_full_close_clears_pnl(self):
        trader = PaperTrader()
        trader.submit_order(order())
        trader.on_tick(tick())
        trader.on_tick(tick(price=90, second=1))
        self.assertEqual(trader.account.floating_pnl, -600)
        self.assertEqual(trader.account.available, trader.account.dynamic_equity - trader.account.margin)
        trader.submit_order(order(direction="SELL", offset="CLOSE", price=90))
        trader.on_tick(tick(price=90, second=2))
        self.assertEqual(trader.account.floating_pnl, 0)
        self.assertEqual(trader.account.balance, Decimal("999400"))

    def test_users_and_restart_preserve_complete_state(self):
        for store in (MemoryStore(), TradingStateStore()):
            import uuid
            user = "restore_" + uuid.uuid4().hex
            registry = PaperAccounts(store)
            a, b = registry.get(user), registry.get(user + "_other")
            o = a.trader.submit_order(order())
            self.assertFalse(b.trader.cancel_order(o.order_id))
            a.on_tick(tick())
            a.stops.submit_stop_order(StopOrderRequest(instrument="JM2701", direction="SELL", trigger_price=90, compare_op="<=", volume=1))
            with a.transaction():
                a.config.enabled = True
            restored = PaperAccounts(store)
            restored.restore_all()
            loaded = restored.get(user)
            self.assertEqual(a.export(), loaded.export())
            self.assertEqual(loaded.trader.get_positions()[0].volume, 1)
            self.assertEqual(len(loaded.trader.trades), 1)
            self.assertTrue(loaded.config.enabled)

    def test_failed_save_rolls_back_funds_and_order(self):
        store = MemoryStore()
        account = PaperAccounts(store).get("alice")
        before = account.export()
        store.fail = True
        with self.assertRaises(RuntimeError):
            account.trader.submit_order(order())
        self.assertEqual(account.export(), before)

    def test_stale_writer_cannot_overwrite_orders(self):
        store = MemoryStore()
        first = PaperAccounts(store).get("alice")
        second = PaperAccounts(store).get("alice")
        first.trader.submit_order(order())
        with self.assertRaises(RuntimeError):
            second.trader.submit_order(order())
        self.assertEqual(len(PaperAccounts(store).get("alice").trader.orders), 1)

    def test_daily_limits_roll_only_on_new_day(self):
        trader = PaperTrader()
        risk = trader.risk_manager
        risk.roll_trading_day("20260914", trader.account)
        trader.account.realized_pnl = Decimal("-60000")
        trader.account.balance -= Decimal("60000")
        self.assertFalse(risk.check_daily_loss_limit(trader.account)[0])
        risk.cancel_counts["JM2701"] = 10
        risk.roll_trading_day("20260915", trader.account)
        self.assertTrue(risk.check_daily_loss_limit(trader.account)[0])
        self.assertFalse(risk.cancel_counts)

    def test_concurrent_orders_reserve_margin_and_position_cap(self):
        from concurrent.futures import ThreadPoolExecutor
        trader = PaperTrader()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: trader.submit_order(order(volume=50)), range(8)))
        self.assertEqual(sum(o.volume for o in trader.active_orders.values()), 200)

    def test_cancel_limit_is_enforced(self):
        trader = PaperTrader()
        trader.risk_manager.max_cancel_count = 1
        self.assertTrue(trader.cancel_order(trader.submit_order(order()).order_id))
        self.assertFalse(trader.cancel_order(trader.submit_order(order()).order_id))

    def test_api_isolates_accounts_conditions_config_and_curves(self):
        from fastapi.testclient import TestClient
        from backend.api.routes import app
        from backend.api.auth import require_user, SessionUser
        from backend.trade_engine.paper_accounts import paper_accounts
        import uuid
        prefix = "api_" + uuid.uuid4().hex
        selected = [prefix + "alice"]
        app.dependency_overrides[require_user] = lambda: SessionUser(selected[0], "sim")
        self.addCleanup(lambda: app.dependency_overrides.pop(require_user, None))
        client = TestClient(app)
        response = client.post("/api/v1/trade/order", json=order().model_dump(mode="json"))
        self.assertEqual(response.status_code, 200)
        order_id = response.json()["order"]["order_id"]
        stop = client.post("/api/v1/trade/stop-order", json={"instrument": "JM2701", "direction": "SELL", "trigger_price": "90", "compare_op": "<=", "volume": 1})
        stop_id = stop.json()["stop_order"]["stop_id"]
        client.post("/api/v1/trade/auto-config", json={"enabled": True})
        client.post("/api/v1/trade/snapshot")
        alice_id = client.get("/api/v1/trade/account").json()["account_id"]
        selected[0] = prefix + "bob"
        self.assertNotEqual(client.get("/api/v1/trade/account").json()["account_id"], alice_id)
        self.assertEqual(client.get("/api/v1/trade/orders").json()["orders"], [])
        self.assertEqual(client.get("/api/v1/trade/stop-orders").json()["stop_orders"], [])
        self.assertFalse(client.get("/api/v1/trade/auto-config").json()["enabled"])
        self.assertEqual(client.get("/api/v1/trade/equity-curve").json()["curve"], [])
        self.assertEqual(client.delete("/api/v1/trade/order/" + order_id).status_code, 400)
        self.assertEqual(client.delete("/api/v1/trade/stop-order/" + stop_id).status_code, 400)
        selected[0] = prefix + "alice"
        paper_accounts.accounts.pop(selected[0])
        self.assertEqual(client.get("/api/v1/trade/orders").json()["orders"][0]["order_id"], order_id)
        self.assertTrue(client.get("/api/v1/trade/auto-config").json()["enabled"])

    def test_account_events_are_not_publicly_broadcast(self):
        from backend.notifier.notifier import EventNotifier
        # 本类已模拟交易通知，使用原始函数验证公共广播边界。
        self.notify_patch.stop()
        with patch("backend.notifier.notifier.threading.Thread"), patch("backend.notifier.notifier.ws_manager.broadcast_threadsafe") as broadcast:
            EventNotifier.notify("TRADE_FILLED", "private trade", source="paper_trader")
            broadcast.assert_not_called()
            EventNotifier.notify("SIGNAL_TRIGGERED", "public market signal", source="signal_engine")
            broadcast.assert_called_once()
