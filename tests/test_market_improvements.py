import test_bootstrap
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from backend.collector.sina_collector import SinaRealtimeCollector, get_watchlist, get_market_subscriptions, WATCHLIST_KEY
from backend.trade_engine.paper_accounts import PaperAccounts, paper_accounts
from backend.trade_engine.state_store import TradingStateStore
from backend.trade_engine.conditional_models import StopOrderRequest
from backend.storage.redis_client import RedisClient
from test_trading_safety import order, tick


class MarketImprovementsTests(unittest.TestCase):
    def setUp(self):
        notify_patch = patch('backend.trade_engine.paper_trader.notifier.notify')
        self.notify = notify_patch.start()
        self.addCleanup(notify_patch.stop)
        self.user = 'market_test_' + uuid.uuid4().hex

    def test_removed_watchlist_keeps_pending_contract_in_background(self):
        from backend.api.routes import remove_watchlist
        redis = RedisClient.get_client()
        redis.sadd(WATCHLIST_KEY, 'JM2701', 'SA2701')
        account = paper_accounts.get(self.user)
        self.addCleanup(lambda: paper_accounts.accounts.pop(self.user, None))
        pending = account.trader.submit_order(order())
        remove_watchlist('JM2701')
        self.assertNotIn('JM2701', redis.smembers(WATCHLIST_KEY))
        self.assertNotIn('JM2701', get_watchlist())
        from backend.api.routes import _ordered_symbols
        self.assertNotIn('JM2701', _ordered_symbols())
        self.assertIn('JM2701', get_market_subscriptions())
        account.on_tick(tick())
        self.assertEqual(account.trader.orders[pending.order_id].status, 'FILLED')
        self.assertIn('JM2701', get_market_subscriptions())
        account.trader.submit_order(order(direction='SELL', offset='CLOSE'))
        account.on_tick(tick(second=1))
        self.assertNotIn('JM2701', get_market_subscriptions())

    def test_only_active_conditions_require_background_subscription(self):
        account = paper_accounts.get(self.user)
        self.addCleanup(lambda: paper_accounts.accounts.pop(self.user, None))
        redis = RedisClient.get_client()
        redis.sadd(WATCHLIST_KEY, 'SA2701')
        redis.srem(WATCHLIST_KEY, 'JM2701')
        stop = account.stops.submit_stop_order(StopOrderRequest(
            instrument='JM2701', direction='BUY', offset='OPEN',
            trigger_price=100, compare_op='>=', volume=1))
        self.assertIn('JM2701', get_market_subscriptions())
        account.stops.cancel_stop_order(stop.stop_id)
        self.assertNotIn('JM2701', get_market_subscriptions())

    def test_cffex_holiday_and_session_boundaries(self):
        cases = [('2026-10-01T10:00:00', 'IF2610', False),
                 ('2026-09-30T09:29:59', 'IF2610', False),
                 ('2026-09-30T09:30:00', 'IF2610', True),
                 ('2026-09-30T15:00:00', 'IF2610', False),
                 ('2026-09-30T15:10:00', 'T2612', True),
                 ('2026-09-30T15:15:00', 'T2612', False)]
        quote = dict(time_str='100000', last_price=100, volume=1, open_interest=1)
        for stamp, symbol, expected in cases:
            with self.subTest(stamp=stamp, symbol=symbol), patch(
                    'backend.collector.sina_collector.datetime') as clock:
                clock.now.return_value = datetime.fromisoformat(stamp)
                collector = SinaRealtimeCollector(lambda _: None)
                self.assertEqual(collector._should_emit(symbol, quote), expected)

    def test_mark_only_save_preserves_history_and_survives_restart(self):
        store = TradingStateStore()
        account = PaperAccounts(store).get(self.user)
        account.trader.submit_order(order())
        account.on_tick(tick())
        history = (account.export()['orders'], account.export()['trades'])
        with patch.object(store, 'save', wraps=store.save) as full, patch.object(
                store, 'save_market', wraps=store.save_market) as partial:
            account.on_tick(tick(price=90, second=1))
            full.assert_not_called()
            partial.assert_called_once()
            self.assertNotIn('orders', partial.call_args.args[1])
            self.assertNotIn('trades', partial.call_args.args[1])
        restored = PaperAccounts(store).get(self.user)
        self.assertEqual(account.export(), restored.export())
        self.assertEqual((restored.export()['orders'], restored.export()['trades']), history)

    def test_mark_save_failure_rolls_back_and_stale_writer_is_rejected(self):
        store = TradingStateStore()
        account = PaperAccounts(store).get(self.user)
        account.trader.submit_order(order())
        account.on_tick(tick())
        stale = PaperAccounts(store).get(self.user)
        before = account.export()
        with patch.object(store, 'save_market', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError):
                account.on_tick(tick(price=90, second=1))
        self.assertEqual(account.export(), before)
        account.on_tick(tick(price=90, second=1))
        with self.assertRaises(RuntimeError):
            stale.on_tick(tick(price=80, second=2))
        self.assertEqual(PaperAccounts(store).get(self.user).export(), account.export())

    def test_condition_tick_uses_full_save(self):
        store = TradingStateStore()
        account = PaperAccounts(store).get(self.user)
        account.trader.submit_order(order())
        account.on_tick(tick())
        account.stops.submit_stop_order(StopOrderRequest(
            instrument='JM2701', direction='SELL', trigger_price=90,
            compare_op='<=', volume=1))
        with patch.object(store, 'save', wraps=store.save) as full, patch.object(
                store, 'save_market', wraps=store.save_market) as partial:
            account.on_tick(tick(price=90, second=1))
            full.assert_called_once()
            partial.assert_not_called()
        self.assertEqual(account.export(), PaperAccounts(store).get(self.user).export())

    def test_history_pages_and_status_filter_are_user_isolated(self):
        from fastapi.testclient import TestClient
        from backend.api.routes import app
        from backend.api.auth import require_user, SessionUser
        account = paper_accounts.get(self.user)
        self.addCleanup(lambda: paper_accounts.accounts.pop(self.user, None))
        app.dependency_overrides[require_user] = lambda: SessionUser(self.user, 'sim')
        self.addCleanup(lambda: app.dependency_overrides.pop(require_user, None))
        ids = []
        for second in range(4):
            ids.append(account.trader.submit_order(order()).order_id)
            account.on_tick(tick(second=second))
        pending = account.trader.submit_order(order(price=90))
        client = TestClient(app)
        page = client.get('/api/v1/trade/orders?limit=2&offset=1').json()['orders']
        self.assertEqual([row['order_id'] for row in page], ids[-2:])
        active = client.get('/api/v1/trade/orders?statuses=SUBMITTED,PARTIAL').json()['orders']
        self.assertEqual([row['order_id'] for row in active], [pending.order_id])
        trades = client.get('/api/v1/trade/trades?limit=2&offset=1').json()['trades']
        self.assertEqual([row['order_id'] for row in trades], ids[1:3])
        self.assertEqual(client.get('/api/v1/trade/orders?statuses=INVALID').status_code, 422)
        self.assertEqual(client.get('/api/v1/trade/trades?limit=0').status_code, 422)
        app.dependency_overrides[require_user] = lambda: SessionUser(self.user + '_other', 'sim')
        self.addCleanup(lambda: paper_accounts.accounts.pop(self.user + '_other', None))
        self.assertEqual(client.get('/api/v1/trade/orders?limit=2').json()['orders'], [])
