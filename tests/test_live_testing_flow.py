"""实盘测试流程新增能力的单元测试。

覆盖:
- 配置档案隔离 (profile) 与 fail-fast
- 风控阈值外置 + 新增检查项 (偏离保护/白名单/日笔数/总开关/软开关)
- 平仓豁免回归 (绝不允许出现"能开不能平")
- 结构化流水表 (order_record / trade_record / position_snapshot) 双写与幂等
- 紧急停机三开关 (禁开仓 / 批量撤单 / 一键全平)
- 告警通道隔离 (交易类事件不得进入公共 WS)
"""
import copy
import test_bootstrap  # noqa: F401  必须最先导入以隔离环境
import os
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from backend.trade_engine.models import (
    OrderRequest, Direction, OffsetFlag, AccountData, PositionData,
)
from backend.trade_engine.risk_manager import PreTradeRiskManager
from backend.trade_engine.live_trader import LiveTrader
from test_trading_safety import MemoryStore, order


def _account(available=1000000, balance=1000000, realized=0):
    return AccountData(balance=Decimal(str(balance)), available=Decimal(str(available)),
                       realized_pnl=Decimal(str(realized)))


def _open(instrument="FU2610", direction=Direction.BUY, price="3000", volume=1, offset=OffsetFlag.OPEN):
    return OrderRequest(instrument=instrument, direction=direction, offset=offset,
                        price=Decimal(price), volume=volume)


# --------------------------------------------------------------------------- #
# 一、配置档案隔离
# --------------------------------------------------------------------------- #

class TestProfileIsolation(unittest.TestCase):
    def test_default_profile_is_local(self):
        from backend.common.config import cfg
        self.assertIn(cfg.system.profile, ("local", "simnow", "prod"))

    def test_invalid_profile_fails_fast(self):
        from backend.common import config as config_module
        original = config_module.resolve_profile
        env_backup = os.environ.get("FUTURES_PROFILE")
        os.environ["FUTURES_PROFILE"] = "not_a_real_profile"
        try:
            with self.assertRaises(RuntimeError):
                config_module.resolve_profile()
        finally:
            if env_backup is None:
                os.environ.pop("FUTURES_PROFILE", None)
            else:
                os.environ["FUTURES_PROFILE"] = env_backup

    def test_profile_files_exist_and_isolate_flow_path(self):
        from pathlib import Path
        from backend.common.config import CONFIG_DIR
        simnow = CONFIG_DIR / "settings.simnow.yaml"
        prod = CONFIG_DIR / "settings.prod.yaml"
        self.assertTrue(simnow.exists(), "缺少 settings.simnow.yaml")
        self.assertTrue(prod.exists(), "缺少 settings.prod.yaml")
        # 仿真与生产的流文件目录必须物理隔离, 否则报单引用序列会混用
        self.assertIn("sim", simnow.read_text(encoding="utf-8"))
        self.assertIn("prod", prod.read_text(encoding="utf-8"))

    def test_prod_profile_defaults_to_live_disabled(self):
        import yaml
        from backend.common.config import CONFIG_DIR
        data = yaml.safe_load((CONFIG_DIR / "settings.prod.yaml").read_text(encoding="utf-8"))
        self.assertFalse(data["risk"]["live_enabled"],
                         "生产档案默认必须 live_enabled=false (fail-safe)")


# --------------------------------------------------------------------------- #
# 二、风控阈值外置与新增检查项
# --------------------------------------------------------------------------- #

class TestRiskConfigExternalised(unittest.TestCase):
    def test_thresholds_read_from_config_not_hardcoded(self):
        from backend.common.config import cfg
        rm = PreTradeRiskManager()
        self.assertEqual(rm.max_order_volume, cfg.risk.max_order_volume)
        self.assertEqual(rm.max_position_volume, cfg.risk.max_position_volume)
        self.assertEqual(rm.margin_ratio, Decimal(str(cfg.risk.margin_ratio)))

    def test_explicit_args_still_override_config(self):
        rm = PreTradeRiskManager(max_order_volume=7, max_position_volume=9,
                                 margin_ratio=Decimal("0.2"), max_cancel_count=11)
        self.assertEqual((rm.max_order_volume, rm.max_position_volume), (7, 9))
        self.assertEqual(rm.margin_ratio, Decimal("0.2"))
        self.assertEqual(rm.max_cancel_count, 11)

    def test_live_disabled_blocks_open_but_allows_close(self):
        rm = PreTradeRiskManager(live_enabled=False, max_order_volume=1)
        acct = _account()
        ok, why = rm.check_order(_open(), acct, {}, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("总开关", why)
        pos = {"FU2610_SELL": PositionData(instrument="FU2610", direction=Direction.SELL, volume=5)}
        close = OrderRequest(instrument="FU2610", direction=Direction.BUY,
                             offset=OffsetFlag.CLOSE, price=Decimal("3000"), volume=5)
        ok, why = rm.check_order(close, acct, pos, {}, last_price=Decimal("3000"))
        self.assertTrue(ok, f"平仓不应被总开关拦截: {why}")

    def test_manual_open_block_is_runtime_and_close_exempt(self):
        rm = PreTradeRiskManager(live_enabled=True)
        rm.block_open("演练")
        acct = _account()
        ok, why = rm.check_order(_open(), acct, {}, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("人工禁止", why)
        pos = {"FU2610_SELL": PositionData(instrument="FU2610", direction=Direction.SELL, volume=5)}
        close = OrderRequest(instrument="FU2610", direction=Direction.BUY,
                             offset=OffsetFlag.CLOSE, price=Decimal("3000"), volume=5)
        self.assertTrue(rm.check_order(close, acct, pos, {}, last_price=Decimal("3000"))[0])
        rm.unblock_open()
        self.assertTrue(rm.check_order(_open(), acct, {}, {}, last_price=Decimal("3000"))[0])

    def test_fat_finger_guard_rejects_deviation(self):
        rm = PreTradeRiskManager(live_enabled=True, fat_finger_pct=Decimal("0.02"))
        acct = _account()
        ok, why = rm.check_order(_open(price="3200"), acct, {}, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("偏离", why)
        # 阈值内应放行
        self.assertTrue(rm.check_order(_open(price="3005"), acct, {}, {}, last_price=Decimal("3000"))[0])

    def test_fat_finger_skipped_when_no_last_price(self):
        rm = PreTradeRiskManager(live_enabled=True, fat_finger_pct=Decimal("0.02"))
        self.assertTrue(rm.check_order(_open(price="9999"), _account(), {}, {}, last_price=None)[0])

    def test_whitelist_blocks_open_but_exempts_close(self):
        rm = PreTradeRiskManager(live_enabled=True, allowed_instruments=["FU2610"], max_order_volume=1)
        acct = _account()
        ok, why = rm.check_order(_open(instrument="BU2610"), acct, {}, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("白名单", why)
        # 白名单外合约的平仓, 且手数超过单笔上限 => 必须放行 (回归"能开不能平"历史故障)
        pos = {"BU2610_SELL": PositionData(instrument="BU2610", direction=Direction.SELL, volume=5)}
        close = OrderRequest(instrument="BU2610", direction=Direction.BUY,
                             offset=OffsetFlag.CLOSE, price=Decimal("3000"), volume=5)
        self.assertTrue(rm.check_order(close, acct, pos, {}, last_price=Decimal("3000"))[0])

    def test_daily_order_limit(self):
        rm = PreTradeRiskManager(live_enabled=True, allowed_instruments=["FU2610"],
                                 max_order_volume=1, daily_order_limit=2)
        rm.trading_day = "20260915"
        rm.day_start_equity = Decimal("1000000")
        acct = _account()
        for _ in range(2):
            self.assertTrue(rm.check_order(_open(), acct, {}, {}, last_price=Decimal("3000"))[0])
            rm.record_order()
        ok, why = rm.check_order(_open(), acct, {}, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("上限", why)

    def test_open_counts_only_reset_on_new_day(self):
        rm = PreTradeRiskManager(live_enabled=True)
        rm.trading_day = "20260915"
        rm.record_order()
        rm.roll_trading_day("20260916", _account())
        self.assertEqual(rm.trading_day, "20260916")
        self.assertEqual(rm.cancel_counts, {})

    def test_existing_open_limits_still_enforced(self):
        """新逻辑不得破坏既有六项风控。"""
        rm = PreTradeRiskManager(live_enabled=True, max_order_volume=1,
                                 max_position_volume=2, allowed_instruments=["FU2610"])
        acct = _account()
        ok, why = rm.check_order(_open(volume=2), acct, {}, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("单笔手数", why)
        pos = {"FU2610_BUY": PositionData(instrument="FU2610", direction=Direction.BUY, volume=2)}
        ok, why = rm.check_order(_open(volume=1), acct, pos, {}, last_price=Decimal("3000"))
        self.assertFalse(ok)
        self.assertIn("持仓上限", why)


# --------------------------------------------------------------------------- #
# 三、结构化流水表双写
# --------------------------------------------------------------------------- #

class TestStructuredLedger(unittest.TestCase):
    def _executor(self):
        from types import SimpleNamespace
        ex = Mock()
        ex.spi = SimpleNamespace(is_connected=True, is_logged_in=True, is_authenticated=True,
                                 settlement_confirmed=True, trading_day="20260915",
                                 front_id=1, session_id=2)
        ex.is_ready = True
        ex.reserve_order_ref = Mock(return_value="9001")
        ex.submit_order = Mock(return_value={"code": 0})
        ex.cancel_order = Mock(return_value={"code": 0})
        ex.request_reconcile = Mock()
        return ex

    def _trader(self):
        ex = self._executor()
        store = MemoryStore()
        trader = LiveTrader(ex, store)
        # 默认档案 live_enabled=false 是 fail-safe 设计; 测试报单链路需显式开启
        trader.risk_manager.live_enabled = True
        trader.reconcile({
            "account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 0,
                         "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
            "positions": [], "orders": [], "trades": [],
        }, "20260915")
        ex.reconciled = True
        ex.last_sync = time.monotonic()
        return trader, ex

    def test_order_written_to_structured_table(self):
        from backend.storage.mysql_client import engine
        from sqlalchemy import text
        trader, ex = self._trader()
        with patch("backend.trade_engine.live_trader.live_ledger.record_order") as rec:
            trader.submit_live_order(order(), "alice")
            self.assertTrue(rec.called, "提交后必须把委托写入结构化流水")
            record = rec.call_args.args[0]
            self.assertEqual(record["status"], "SUBMITTED")
            self.assertEqual(record["instrument"], "JM2701")

    def test_trade_written_and_idempotent(self):
        from backend.trade_engine import live_ledger
        from backend.storage.mysql_client import engine
        from sqlalchemy import text
        trader, ex = self._trader()
        trade = dict(TradingDay="20260915", ExchangeID="DCE", TradeID="T-LEDGER-1",
                     OrderRef="9001", InstrumentID="jm2701", Direction="0",
                     OffsetFlag="0", Price=100, Volume=2,
                     TradeDate="20260915", TradeTime="09:30:00")
        trader.on_trade(trade)
        trader.on_trade(trade)   # 重复回报
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT COUNT(*), SUM(volume) FROM trade_record "
                                     "WHERE trade_id = 'T-LEDGER-1'")).fetchone()
        self.assertEqual(rows[0], 1, "重复 TradeID 必须幂等, 不能产生两行")
        self.assertEqual(int(rows[1]), 2)

    def test_ledger_failure_does_not_break_trading(self):
        """落库是旁路: 结构化写失败绝不能阻断报单。"""
        trader, ex = self._trader()
        with patch("backend.trade_engine.live_ledger.record_order",
                   side_effect=RuntimeError("db down")):
            result = trader.submit_live_order(order(), "alice")
        self.assertEqual(result["status"], "SUBMITTED")
        self.assertEqual(ex.submit_order.call_count, 1)

    def test_position_snapshot_recorded_on_reconcile(self):
        trader, ex = self._trader()
        with patch("backend.trade_engine.live_ledger.record_positions") as rec:
            trader.reconcile({
                "account": [{"Balance": 1, "Available": 1, "CurrMargin": 0, "FrozenMargin": 0,
                             "CloseProfit": 0, "PreBalance": 1}],
                "positions": [{"InstrumentID": "fu2610", "PosiDirection": "2", "Position": 1,
                               "TodayPosition": 1, "YdPosition": 0, "LongFrozen": 0,
                               "ShortFrozen": 0, "UseMargin": 3600}],
                "orders": [], "trades": [],
            }, "20260915")
            self.assertTrue(rec.called)
            self.assertEqual(rec.call_args.args[1], "20260915")


# --------------------------------------------------------------------------- #
# 四、紧急停机三开关
# --------------------------------------------------------------------------- #

class TestEmergencySwitches(unittest.TestCase):
    def _trader(self):
        from types import SimpleNamespace
        ex = Mock()
        ex.spi = SimpleNamespace(is_connected=True, is_logged_in=True, is_authenticated=True,
                                 settlement_confirmed=True, trading_day="20260915",
                                 front_id=1, session_id=2)
        ex.is_ready = True
        ex.reserve_order_ref = Mock(side_effect=lambda: str(9000 + TestEmergencySwitches._n()))
        ex.submit_order = Mock(return_value={"code": 0})
        ex.cancel_order = Mock(return_value={"code": 0})
        ex.request_reconcile = Mock()
        store = MemoryStore()
        trader = LiveTrader(ex, store)
        trader.risk_manager.live_enabled = True
        trader.reconcile({
            "account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 0,
                         "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
            "positions": [], "orders": [], "trades": [],
        }, "20260915")
        ex.reconciled = True
        ex.last_sync = time.monotonic()
        return trader, ex

    _counter = 0

    @classmethod
    def _n(cls):
        cls._counter += 1
        return cls._counter

    def test_switch1_block_open_and_persist_across_restart(self):
        trader, _ = self._trader()
        trader.block_open("演练")
        self.assertTrue(trader.risk_manager.open_blocked)
        restarted = LiveTrader(trader.executor, trader.store)
        restarted._load()   # 真实使用路径: 任何交易动作都会先触发 _load
        self.assertTrue(restarted.risk_manager.open_blocked,
                        "禁开仓状态必须跨重启保留, 否则重启后会自动恢复报单")
        restarted.unblock_open()
        self.assertFalse(restarted.risk_manager.open_blocked)
        third = LiveTrader(trader.executor, trader.store)
        third._load()
        self.assertFalse(third.risk_manager.open_blocked)

    def test_switch2_cancel_all_only_touches_active_orders(self):
        trader, ex = self._trader()
        r1 = trader.submit_live_order(order(), "alice")
        r2 = trader.submit_live_order(order(), "alice")
        # 其中一笔成交, 不应被撤
        trader.on_order(dict(TradingDay="20260915", OrderRef="9001", FrontID=1, SessionID=2,
                             InstrumentID="jm2701", ExchangeID="DCE", OrderSysID="1",
                             Direction="0", CombOffsetFlag="0", LimitPrice=100,
                             VolumeTotalOriginal=1, VolumeTraded=1, OrderStatus="0",
                             OrderSubmitStatus="3", StatusMsg="", InsertDate="20260915",
                             InsertTime="09:30:00"))
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0):
            result = trader.cancel_all("alice")
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["attempted"], 1)
        self.assertLessEqual(result["attempted"], 2)   # 已成交的那笔不应在撤单范围内

    def test_switch3_requires_confirm_string(self):
        trader, ex = self._trader()
        result = trader.close_all("alice", confirm="")
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("确认字符串", result["reason"])
        ex.cancel_order.assert_not_called()

    def test_switch3_closes_positions_with_rate_limit(self):
        trader, ex = self._trader()
        trader.reconcile({
            "account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 3600,
                         "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
            "positions": [{"InstrumentID": "fu2610", "PosiDirection": "2", "Position": 2,
                           "TodayPosition": 2, "YdPosition": 0, "LongFrozen": 0,
                           "ShortFrozen": 0, "UseMargin": 3600}],
            "orders": [], "trades": [],
        }, "20260915")
        ex.reconciled = True
        ex.last_sync = time.monotonic()
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0), \
             patch.object(trader, "_opponent_price", return_value=Decimal("3001")):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL", batch_size=1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["closed"]), 2, "2 手持仓按 batch_size=1 应拆成 2 笔平仓单")
        for item in result["closed"]:
            self.assertEqual(item["direction"], "SELL")   # 平多单 => 卖
        self.assertEqual(ex.submit_order.call_count, 2)

    def test_switch3_is_noop_when_flat(self):
        trader, ex = self._trader()
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["closed"], [])
        ex.submit_order.assert_not_called()

    def test_close_offset_distinguishes_today_and_yesterday_for_shfe(self):
        """上期所必须区分平今/平昨, 否则平仓会被柜台拒绝。"""
        trader, _ = self._trader()
        self.assertEqual(trader._decide_close_offset("FU2610", "FU2610_BUY", 2, {"FU2610_BUY": 5}),
                         OffsetFlag.CLOSE_TODAY)
        self.assertEqual(trader._decide_close_offset("FU2610", "FU2610_BUY", 3, {"FU2610_BUY": 1}),
                         OffsetFlag.CLOSE_TODAY)
        self.assertEqual(trader._decide_close_offset("FU2610", "FU2610_BUY", 2, {"FU2610_BUY": 0}),
                         OffsetFlag.CLOSE_YEST)
        # 其他交易所不区分
        self.assertEqual(trader._decide_close_offset("JM2701", "JM2701_BUY", 2, {"JM2701_BUY": 5}),
                         OffsetFlag.CLOSE)

    def test_close_all_skips_when_no_opponent_price(self):
        """取不到盘口时宁可不平, 也不盲目报一个可能错价成交的限价单。"""
        trader, ex = self._trader()
        trader.reconcile({
            "account": [{"Balance": 1000000, "Available": 1000000, "CurrMargin": 3600,
                         "FrozenMargin": 0, "CloseProfit": 0, "PreBalance": 1000000}],
            "positions": [{"InstrumentID": "fu2610", "PosiDirection": "2", "Position": 1,
                           "TodayPosition": 1, "YdPosition": 0, "LongFrozen": 0,
                           "ShortFrozen": 0, "UseMargin": 3600}],
            "orders": [], "trades": [],
        }, "20260915")
        ex.reconciled = True
        ex.last_sync = time.monotonic()
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0), \
             patch.object(trader, "_opponent_price", return_value=None):
            result = trader.close_all("alice", confirm="CONFIRM_CLOSE_ALL")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["errors"])
        ex.submit_order.assert_not_called()


# --------------------------------------------------------------------------- #
# 五、告警通道隔离
# --------------------------------------------------------------------------- #

class TestAlertChannelIsolation(unittest.TestCase):
    def test_trade_events_never_reach_public_ws(self):
        from backend.notifier.notifier import notifier
        with patch("backend.notifier.notifier.ws_manager.broadcast_threadsafe") as broadcast, \
             patch("backend.notifier.notifier.threading.Thread"):
            notifier.notify("CTP_TRADE_FILLED", "成交", "INFO", "ctp_trader")
            broadcast.assert_not_called()
            broadcast.reset_mock()
            notifier.notify("FEED_SINA_STARTED", "行情启动", "INFO", "main")
            broadcast.assert_called_once()

    def test_private_webhook_separate_from_public(self):
        from backend.notifier.notifier import notifier
        notifier.set_webhook_url("http://public.example/hook")
        notifier.set_private_webhook_url("http://private.example/hook")
        try:
            status = notifier.get_channel_status()
            self.assertTrue(status["public_webhook"])
            self.assertTrue(status["private_webhook"])
            sent = []
            with patch.object(notifier, "_post_webhook", side_effect=lambda url, *a, **k: sent.append(url)), \
                 patch("backend.notifier.notifier.threading.Thread") as thread_cls:
                def run_now(target=None, args=(), **kwargs):
                    if target is not None:
                        target(*args)
                    return Mock()
                thread_cls.side_effect = run_now
                notifier.notify("CTP_TRADE_FILLED", "成交", "INFO", "ctp_trader")
                notifier.notify("FEED_SINA_STARTED", "启动", "INFO", "main")
            self.assertIn("http://private.example/hook", sent)
            self.assertIn("http://public.example/hook", sent)
        finally:
            notifier.set_webhook_url("")
            notifier.set_private_webhook_url("")


if __name__ == "__main__":
    unittest.main()
