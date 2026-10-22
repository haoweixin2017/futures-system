"""回归测试: 实盘「平今/平昨拆单」与「撤单被柜台拒绝」两处实测缺陷。

背景 (均在对仿真柜台的端到端测试中暴露):
1. close_all 对 SHFE/INE 只用一个平仓标志。当今仓手数少于单批手数时, 整笔被
   报成 CLOSE_TODAY, 柜台以「今仓不足」直接拒单, 导致一键全平部分失败。
   代码注释原本写着「拆单更安全」, 但实现并未拆单。
2. 柜台拒绝撤单的回报 (OnRspOrderAction / OnErrRtnOrderAction 携带 ErrorID)
   被静默丢弃: 本地记录停留在 CANCELLING, 操作人既看不到失败原因, 也无法再次撤单。
"""
import test_bootstrap  # noqa: F401  必须最先导入以隔离环境
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.trade_engine.models import Direction, OffsetFlag
from backend.trade_engine.live_trader import LiveTrader
from test_trading_safety import MemoryStore, order


def _trader(positions=None):
    ex = Mock()
    ex.spi = SimpleNamespace(is_connected=True, is_logged_in=True, is_authenticated=True,
                             settlement_confirmed=True, trading_day="20260915",
                             front_id=1, session_id=2)
    ex.is_ready = True
    ex.reconciled = True
    ex.last_sync = time.monotonic()
    counter = {"n": 9000}
    ex.reserve_order_ref = Mock(side_effect=lambda: str(counter.__setitem__("n", counter["n"] + 1) or counter["n"]))
    ex.submit_order = Mock(return_value={"code": 0})
    ex.cancel_order = Mock(return_value={"code": 0})
    ex.request_reconcile = Mock()
    trader = LiveTrader(ex, MemoryStore())
    trader.risk_manager.live_enabled = True
    trader.reconcile({
        "account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 0,
                     "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
        "positions": positions or [], "orders": [], "trades": [],
    }, "20260915")
    return trader, ex


def _shfe_long(position=5, today=1, yd=4):
    return [{"InstrumentID": "fu2610", "PosiDirection": "2", "Position": position,
             "TodayPosition": today, "YdPosition": yd, "LongFrozen": 0, "ShortFrozen": 0,
             "UseMargin": 9000}]


class TestCloseSplitPlan(unittest.TestCase):
    """_plan_close: 保证单条平仓指令的手数不超过对应仓位可用量。"""

    def test_splits_today_and_yesterday_when_today_insufficient(self):
        trader, _ = _trader()
        self.assertEqual(trader._plan_close("FU2610", "FU2610_BUY", 3, {"FU2610_BUY": 1}),
                         [(OffsetFlag.CLOSE_TODAY, 1), (OffsetFlag.CLOSE_YEST, 2)])

    def test_single_close_today_when_today_sufficient(self):
        trader, _ = _trader()
        self.assertEqual(trader._plan_close("FU2610", "FU2610_BUY", 2, {"FU2610_BUY": 5}),
                         [(OffsetFlag.CLOSE_TODAY, 2)])

    def test_close_yesterday_when_no_today_position(self):
        trader, _ = _trader()
        self.assertEqual(trader._plan_close("FU2610", "FU2610_BUY", 2, {"FU2610_BUY": 0}),
                         [(OffsetFlag.CLOSE_YEST, 2)])

    def test_ine_is_treated_like_shfe(self):
        trader, _ = _trader()
        self.assertEqual(trader._plan_close("SC2610", "SC2610_BUY", 4, {"SC2610_BUY": 2}),
                         [(OffsetFlag.CLOSE_TODAY, 2), (OffsetFlag.CLOSE_YEST, 2)])

    def test_other_exchanges_use_plain_close(self):
        trader, _ = _trader()
        self.assertEqual(trader._plan_close("JM2701", "JM2701_BUY", 5, {"JM2701_BUY": 5}),
                         [(OffsetFlag.CLOSE, 5)])

    def test_split_legs_never_exceed_bucket(self):
        trader, _ = _trader()
        for today in (0, 1, 2, 5, 9):
            for volume in (1, 3, 7):
                for offset, leg in trader._plan_close("FU2610", "FU2610_BUY", volume,
                                                      {"FU2610_BUY": today}):
                    if offset == OffsetFlag.CLOSE_TODAY:
                        self.assertLessEqual(leg, today)
                    self.assertGreater(leg, 0)


class TestCloseAllSplitsOrders(unittest.TestCase):
    def test_close_all_emits_today_then_yesterday_legs(self):
        trader, ex = _trader(positions=_shfe_long(position=5, today=1, yd=4))
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0), \
             patch.object(trader, "_opponent_price", return_value=Decimal("3001")):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL", batch_size=3)
        self.assertEqual(result["status"], "success")
        legs = [(c["offset"], c["volume"]) for c in result["closed"]]
        self.assertEqual(legs, [("CLOSE_TODAY", 1), ("CLOSE_YEST", 2), ("CLOSE_YEST", 2)],
                         "今仓 1 手 + 昨仓 4 手, 单批 3 手 => 平今1 + 平昨2 + 平昨2")
        self.assertEqual(sum(v for _, v in legs), 5, "拆单后总手数必须等于可平持仓")
        self.assertEqual(ex.submit_order.call_count, 3)

    def test_close_all_still_works_when_today_covers_batch(self):
        trader, ex = _trader(positions=_shfe_long(position=2, today=2, yd=0))
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0), \
             patch.object(trader, "_opponent_price", return_value=Decimal("3001")):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL", batch_size=1)
        self.assertEqual([(c["offset"], c["volume"]) for c in result["closed"]],
                         [("CLOSE_TODAY", 1), ("CLOSE_TODAY", 1)])

    def test_close_all_skips_instrument_when_no_opponent_price(self):
        trader, ex = _trader(positions=_shfe_long(position=1, today=1, yd=0))
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0), \
             patch.object(trader, "_opponent_price", return_value=None):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL", batch_size=3)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["errors"])
        ex.submit_order.assert_not_called()


class TestCancelRejectionHandling(unittest.TestCase):
    def _cancelling(self, trader):
        rec = trader.submit_live_order(order(), "alice")
        trader._orders[rec["order_ref"]]["status"] = "CANCELLING"
        action = {"OrderRef": rec["gateway_ref"], "FrontID": 1, "SessionID": 2,
                  "InstrumentID": "jm2701", "ExchangeID": "DCE", "OrderSysID": "1"}
        return rec, action

    def test_rejected_cancel_restores_submitted_and_records_reason(self):
        trader, _ = _trader()
        rec, action = self._cancelling(trader)
        trader.on_cancel_rejected(action, "CTP:撤单被拒绝, 委托已成交")
        record = trader._orders[rec["order_ref"]]
        self.assertEqual(record["status"], "SUBMITTED", "撤单失败必须回退为可撤状态")
        self.assertIn("撤单被柜台拒绝", record["reject_reason"])
        self.assertIn("委托已成交", record["reject_reason"])

    def test_rejected_cancel_does_not_clobber_terminal_status(self):
        trader, _ = _trader()
        rec, action = self._cancelling(trader)
        trader._orders[rec["order_ref"]]["status"] = "FILLED"
        trader.on_cancel_rejected(action, "已成交")
        self.assertEqual(trader._orders[rec["order_ref"]]["status"], "FILLED")

    def test_rejected_cancel_for_unknown_order_is_ignored(self):
        trader, _ = _trader()
        before = len(trader._orders)
        trader.on_cancel_rejected({"OrderRef": "999999", "FrontID": 1, "SessionID": 2},
                                  "找不到委托")
        self.assertEqual(len(trader._orders), before, "未知委托不得凭空建记录")

    def test_executor_cancel_rejected_delegates(self):
        from backend.trade_engine.ctp_trader import CTPTradeExecutor
        ex = CTPTradeExecutor.__new__(CTPTradeExecutor)
        ex.sync_error = ""
        action = SimpleNamespace(OrderRef="2", FrontID=1, SessionID=2, InstrumentID="fu2610",
                                 ExchangeID="SHFE", OrderSysID="1")
        with patch("backend.trade_engine.live_trader.live_trader.on_cancel_rejected") as cb:
            ex.cancel_rejected(action, "柜台拒绝")
        cb.assert_called_once()
        self.assertEqual(cb.call_args.args[1], "柜台拒绝")

    def test_executor_cancel_rejected_never_raises(self):
        from backend.trade_engine.ctp_trader import CTPTradeExecutor
        ex = CTPTradeExecutor.__new__(CTPTradeExecutor)
        ex.sync_error = ""
        with patch("backend.trade_engine.live_trader.live_trader.on_cancel_rejected",
                   side_effect=RuntimeError("db down")):
            ex.cancel_rejected(SimpleNamespace(OrderRef="2"), "拒绝")
        self.assertTrue(ex.sync_error, "回报处理异常必须被记录, 但不能向外抛出")


if __name__ == "__main__":
    unittest.main()
