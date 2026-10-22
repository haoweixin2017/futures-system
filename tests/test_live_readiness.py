"""回归测试: 实盘柜台就绪状态 (is_ready) 不得因"请求刷新快照"而翻转。

背景 (实测于仿真柜台):
CTPTradeExecutor._sync_loop 每轮对账开始时无条件把 reconciled 置为 False, 而
request_reconcile() 也会清零它 —— 但周期心跳与每一次报单/成交回报都会调用后者。
后果:
1. is_ready 随 5 秒心跳反复翻转, 实测约一半时间不可用, 单笔报单被随机拒绝
   ("CTP 实盘柜台未连接或尚未完成结算确认与对账");
2. 批量报单 (一键全平) 只有第一笔能通过, 后续全部被拒 —— 紧急退出最不能失效的场景。

修复后语义:
- request_reconcile(): 只请求刷新快照, 不影响就绪状态;
- mark_uncertain(): 发送/撤单结果不明时才置为未就绪, 直到对账澄清;
- 对账失败或存在未确认报单: 仍然置为未就绪 (安全侧不变)。
"""
import test_bootstrap  # noqa: F401  必须最先导入以隔离环境
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.trade_engine.ctp_trader import CTPTradeExecutor
from backend.trade_engine.live_trader import LiveTrader
from test_trading_safety import MemoryStore, order


def _account(positions=None):
    return {"account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 0,
                         "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
            "positions": positions or [], "orders": [], "trades": []}


def _real_executor_trader(positions=None):
    """使用真实的 CTPTradeExecutor 就绪语义 (只桩掉发送动作)。"""
    ex = CTPTradeExecutor()
    ex.spi = SimpleNamespace(is_connected=True, is_logged_in=True, is_authenticated=True,
                             settlement_confirmed=True, trading_day="20260915",
                             front_id=1, session_id=2)
    counter = {"n": 9000}

    def _next_ref():
        counter["n"] += 1
        return str(counter["n"])

    ex.reserve_order_ref = Mock(side_effect=_next_ref)
    ex.submit_order = Mock(return_value={"code": 0})
    ex.cancel_order = Mock(return_value={"code": 0})
    trader = LiveTrader(ex, MemoryStore())
    trader.risk_manager.live_enabled = True
    trader.reconcile(_account(positions), "20260915")
    ex.reconciled = True
    ex.last_sync = time.monotonic()
    return trader, ex


class TestReadinessSemantics(unittest.TestCase):
    def _executor(self):
        ex = CTPTradeExecutor()
        ex.spi = SimpleNamespace(is_connected=True, is_logged_in=True, is_authenticated=True,
                                 settlement_confirmed=True)
        ex.reconciled = True
        ex.last_sync = time.monotonic()
        return ex

    def test_request_reconcile_does_not_disable_readiness(self):
        ex = self._executor()
        self.assertTrue(ex.is_ready)
        ex.request_reconcile()
        self.assertTrue(ex.is_ready, "仅请求刷新快照不得使柜台变为未就绪")

    def test_mark_uncertain_disables_readiness(self):
        ex = self._executor()
        ex.mark_uncertain()
        self.assertFalse(ex.is_ready, "结果不确定必须阻断报单")

    def test_readiness_requires_fresh_sync(self):
        ex = self._executor()
        ex.last_sync = time.monotonic() - (ex.SYNC_STALE_SECONDS + 1)
        self.assertFalse(ex.is_ready, "对账过期必须阻断报单")

    def test_readiness_requires_login_and_settlement(self):
        ex = self._executor()
        ex.spi.settlement_confirmed = False
        self.assertFalse(ex.is_ready)
        ex.spi.settlement_confirmed = True
        ex.spi.is_logged_in = False
        self.assertFalse(ex.is_ready)


class TestBatchOrdersNotBlockedByReconcile(unittest.TestCase):
    def test_second_order_is_not_rejected_after_first(self):
        trader, ex = _real_executor_trader()
        first = trader.submit_live_order(order(), "alice")
        second = trader.submit_live_order(order(), "alice")
        self.assertEqual(first["status"], "SUBMITTED", first.get("reject_reason"))
        self.assertEqual(second["status"], "SUBMITTED",
                         f"第一笔报单触发的对账请求不得使第二笔被拒: {second.get('reject_reason')}")
        self.assertEqual(ex.submit_order.call_count, 2)

    def test_close_all_submits_every_leg(self):
        trader, ex = _real_executor_trader(positions=[
            {"InstrumentID": "fu2610", "PosiDirection": "2", "Position": 2,
             "TodayPosition": 2, "YdPosition": 0, "LongFrozen": 0, "ShortFrozen": 0,
             "UseMargin": 3600}])
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0), \
             patch.object(trader, "_opponent_price", return_value=Decimal("3001")):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL", batch_size=1)
        self.assertEqual(result["status"], "success", result.get("errors"))
        self.assertEqual(len(result["closed"]), 2,
                         "2 手持仓拆成 2 笔平仓单, 第二笔不得因对账请求被拒")
        for item in result["errors"]:
            self.assertNotIn("柜台未连接或尚未完成", item.get("reason", ""))
        self.assertEqual(ex.submit_order.call_count, 2)

    def test_uncertain_send_blocks_following_orders(self):
        trader, ex = _real_executor_trader()
        ex.submit_order.side_effect = TimeoutError()
        first = trader.submit_live_order(order(), "alice")
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertFalse(ex.is_ready, "结果不明的报单必须立即阻断后续报单")
        ex.submit_order.side_effect = None
        ex.submit_order.return_value = {"code": 0}
        second = trader.submit_live_order(order(), "alice")
        self.assertEqual(second["status"], "REJECTED")
        self.assertIn("尚未完成结算确认与对账", second["reject_reason"])


if __name__ == "__main__":
    unittest.main()
