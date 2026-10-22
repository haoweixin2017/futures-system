import test_bootstrap
import unittest
import time
from types import SimpleNamespace
from unittest.mock import Mock
from backend.trade_engine.ctp_trader import CTPTradeExecutor, CTPTraderHandler
from backend.trade_engine.live_trader import LiveTrader
from test_trading_safety import MemoryStore, order


def account_snapshot(orders=None, trades=None):
    return {"account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 0,
                         "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
            "positions": [], "orders": orders or [], "trades": trades or []}


def order_row(ref="1001", status="0", traded=1):
    return dict(TradingDay="20260914", OrderRef=ref, FrontID=1, SessionID=2,
                InstrumentID="jm2701", ExchangeID="DCE", OrderSysID="123",
                Direction="0", CombOffsetFlag="0", LimitPrice=100,
                VolumeTotalOriginal=1, VolumeTraded=traded, OrderStatus=status,
                OrderSubmitStatus="3", StatusMsg="", InsertDate="20260914", InsertTime="09:00:00")


class TestLiveReconciliation(unittest.TestCase):
    def setUp(self):
        self.executor = CTPTradeExecutor()
        self.executor.spi = SimpleNamespace(is_connected=True, is_logged_in=True,
            is_authenticated=True, settlement_confirmed=True, trading_day="20260914", front_id=1, session_id=2)
        self.executor.reserve_order_ref = Mock(return_value="1001")
        self.executor.submit_order = Mock(return_value={"code": 0})
        self.executor.cancel_order = Mock(return_value={"code": 0})
        self.store = MemoryStore()
        self.trader = LiveTrader(self.executor, self.store)
        # risk.live_enabled 默认 false 是 fail-safe 设计 (生产档案默认禁止报单);
        # 本用例验证的是报单链路本身, 故显式开启。
        self.trader.risk_manager.live_enabled = True
        self.trader.reconcile(account_snapshot(), "20260914")
        self.ready()

    def ready(self):
        self.executor.reconciled = True
        self.executor.last_sync = time.monotonic()

    def test_login_alone_does_not_allow_order(self):
        self.executor.spi.settlement_confirmed = False
        self.assertFalse(self.executor.is_ready)
        result = self.trader.submit_live_order(order(), "alice")
        self.assertEqual(result["status"], "REJECTED")
        self.executor.submit_order.assert_not_called()

    def test_intent_persisted_before_send_and_reconciles_after_restart(self):
        def send(req, order_ref):
            data, _ = self.store.load(self.trader.key)
            self.assertEqual(next(iter(data["orders"].values()))["status"], "SENDING")
            return {"code": 0}
        self.executor.submit_order.side_effect = send
        result = self.trader.submit_live_order(order(), "alice")
        self.assertEqual(result["status"], "SUBMITTED")
        restarted = LiveTrader(self.executor, self.store)
        restarted.reconcile(account_snapshot([order_row()]), "20260914")
        self.assertEqual(restarted.get_orders("alice")[0]["status"], "FILLED")
        self.assertFalse(restarted.get_orders("bob"))

    def test_rejected_callback_and_duplicate_trade_are_persisted(self):
        self.trader.submit_live_order(order(), "alice")
        row = order_row(status="5", traded=0)
        row["OrderSubmitStatus"] = "4"
        self.trader.on_order(row)
        self.assertEqual(self.trader.get_orders("alice")[0]["status"], "REJECTED")
        trade = dict(TradingDay="20260914", ExchangeID="DCE", TradeID="T1", OrderSysID="123", Price=100, Volume=1)
        self.trader.on_trade(trade)
        self.trader.on_trade(trade)
        self.assertEqual(len(LiveTrader(self.executor, self.store).get_snapshot()["trades"]), 1)

    def test_unknown_send_is_never_retried_or_marked_rejected(self):
        self.executor.submit_order.side_effect = TimeoutError()
        result = self.trader.submit_live_order(order(), "alice")
        self.assertEqual(result["status"], "UNKNOWN")
        with self.assertRaises(RuntimeError):
            self.trader.reconcile(account_snapshot(), "20260914")
        self.assertFalse(self.executor.is_ready)
        self.assertEqual(self.executor.submit_order.call_count, 1)

    def test_persistence_failure_prevents_send(self):
        self.store.fail = True
        with self.assertRaises(RuntimeError):
            self.trader.submit_live_order(order(), "alice")
        self.executor.submit_order.assert_not_called()
        self.assertFalse(self.executor.is_ready)

    def test_cancel_requires_owner_and_uses_original_identity(self):
        result = self.trader.submit_live_order(order(), "alice")
        self.assertEqual(self.trader.cancel_live_order(result["order_ref"], "bob")["status"], "REJECTED")
        self.executor.cancel_order.assert_not_called()
        cancelled = self.trader.cancel_live_order(result["order_ref"], "alice")
        self.assertEqual(cancelled["status"], "CANCELLING")
        self.assertEqual(self.executor.cancel_order.call_args.kwargs["record"]["front_id"], 1)

    def test_late_submitted_callback_does_not_regress_fill(self):
        self.trader.submit_live_order(order(), "alice")
        self.trader.on_order(order_row())
        self.trader.on_order(order_row(status="3", traded=0))
        result = self.trader.get_orders("alice")[0]
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["traded_volume"], 1)

    def test_ctp_callback_requires_successful_settlement(self):
        handler = CTPTraderHandler(Mock())
        handler.executor = Mock()
        handler.OnRspSettlementInfoConfirm(None, SimpleNamespace(ErrorID=1), 3, True)
        self.assertFalse(handler.settlement_confirmed)
        handler.OnRspSettlementInfoConfirm(None, SimpleNamespace(ErrorID=0), 3, True)
        self.assertTrue(handler.settlement_confirmed)
        handler.executor.request_reconcile.assert_called_once()
