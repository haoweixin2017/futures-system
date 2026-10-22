"""模拟盘紧急全平与账务边界的回归测试。

背景: 实测发现 `close_all_positions` 只要遇到一个缺行情的品种就 raise,
而 `@atomic` 事务装饰器会因此把整轮全平回滚 —— 已能平的品种也平不掉。
紧急平仓的正确行为必须是"能平的先平", 不能被单个品种拖死。
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import test_bootstrap  # noqa: F401  必须最先导入以完成测试环境隔离

from backend.models.market_data import TickData
from backend.trade_engine.models import Direction, OffsetFlag, OrderRequest, OrderStatus
from backend.trade_engine.paper_trader import PaperTrader


def make_tick(sym, price, sec=0):
    return TickData(
        instrument=sym, exchange="CZCE", trading_day="20260914",
        timestamp=datetime(2026, 9, 14, 10, 0) + timedelta(seconds=sec),
        last_price=Decimal(str(price)),
        bid_price_1=Decimal(str(price)), bid_volume_1=1,
        ask_price_1=Decimal(str(price)), ask_volume_1=1,
        volume=100, turnover=Decimal("1000"), open_interest=Decimal("1000"),
    )


class EmergencyCloseTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(Decimal("1000000"))
        self.A, self.B = "SA2701", "FU2611"

    def _open(self, sym, price, sec, volume=1):
        self.trader.submit_order(OrderRequest(
            instrument=sym, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal(str(price)), volume=volume))
        self.trader.on_tick(make_tick(sym, price, sec))

    def test_closes_all_when_quotes_available(self):
        self._open(self.A, 1000, 1)
        self._open(self.B, 4000, 2)
        ids = self.trader.close_all_positions()
        self.assertEqual(len(ids), 2, "两个品种都应提交平仓委托")

    def test_skips_symbol_without_quote_and_keeps_others(self):
        """核心回归: 一个品种无行情不应阻止其他品种平仓。"""
        self._open(self.A, 1000, 1)
        self._open(self.B, 4000, 2)
        del self.trader.last_prices[self.B]

        ids = self.trader.close_all_positions()  # 不应抛异常

        self.assertEqual(len(ids), 1, "有行情的品种仍应被提交平仓")
        # A 已提交平仓 -> 产生一笔 CLOSE 委托
        close_orders = [o for o in self.trader.orders.values()
                        if o.instrument == self.A and o.offset == OffsetFlag.CLOSE]
        self.assertEqual(len(close_orders), 1)

    def test_no_quote_does_not_raise_value_error(self):
        """旧实现在此抛 ValueError 并导致事务回滚。"""
        self._open(self.A, 1000, 1)
        del self.trader.last_prices[self.A]
        try:
            self.trader.close_all_positions()
        except ValueError:
            self.fail("缺行情不应再抛 ValueError (会导致整轮回滚)")
        except Exception as e:
            self.fail(f"不应抛异常, 实际: {type(e).__name__}: {e}")

    def test_all_frozen_position_is_skipped_not_fatal(self):
        """持仓全部冻结 (无可平手数) 应跳过该品种, 不影响其余。"""
        self._open(self.A, 1000, 1)
        self._open(self.B, 4000, 2)
        # 冻结 B 的全部持仓
        self.trader.positions[self.trader.get_pos_key(self.B, Direction.BUY)].frozen_volume = 1

        ids = self.trader.close_all_positions()
        self.assertEqual(len(ids), 1, "A 应正常提交, B 被跳过")
        self.assertEqual(ids[0], self.trader.orders[ids[0]].order_id)

    def test_rejected_close_order_is_skipped(self):
        """submit_order 返回 REJECTED 时不应计入已提交列表。"""
        self._open(self.A, 1000, 1)
        real_submit = self.trader.submit_order
        calls = {"n": 0}

        def fake_submit(req):
            calls["n"] += 1
            order = real_submit(req)
            order.status = OrderStatus.REJECTED
            order.reject_reason = "模拟拒绝"
            return order

        with patch.object(self.trader, "submit_order", side_effect=fake_submit):
            ids = self.trader.close_all_positions()
        self.assertEqual(ids, [], "被拒绝的委托不应算作已提交")

    def test_notification_raised_when_partially_closed(self):
        """部分未能平仓时必须发出告警, 不能静默。"""
        self._open(self.A, 1000, 1)
        self._open(self.B, 4000, 2)
        del self.trader.last_prices[self.B]
        with patch("backend.trade_engine.paper_trader.notifier") as notify:
            self.trader.close_all_positions()
        notify.notify.assert_called_once()
        self.assertEqual(notify.notify.call_args[0][0], "EMERGENCY_CLOSE_PARTIAL")

    def test_no_notification_when_all_closed(self):
        self._open(self.A, 1000, 1)
        with patch("backend.trade_engine.paper_trader.notifier") as notify:
            self.trader.close_all_positions()
        notify.notify.assert_not_called()


class PaperTradingAccountingTests(unittest.TestCase):
    """账务自洽性: 权益 = 余额 + 浮盈; 可用 = 权益 - 保证金 - 冻结。"""

    def setUp(self):
        self.trader = PaperTrader(Decimal("1000000"))
        self.SYM = "SA2701"
        self.MULT = self.trader.get_instrument_multiplier(self.SYM)

    def _open(self, price, volume, sec):
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal(str(price)), volume=volume))
        self.trader.on_tick(make_tick(self.SYM, price, sec))

    def test_equity_identity_holds(self):
        self._open(1000, 2, 1)
        a = self.trader.account
        self.assertEqual(a.dynamic_equity, a.balance + a.floating_pnl)

    def test_available_identity_holds(self):
        self._open(1000, 2, 1)
        a = self.trader.account
        self.assertEqual(a.available, a.dynamic_equity - a.margin - a.frozen_margin)

    def test_realized_pnl_uses_contract_multiplier(self):
        """平仓盈亏必须乘合约乘数 (SA 为 20 元/点)。"""
        self._open(1000, 1, 1)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1100"), volume=1))
        self.trader.on_tick(make_tick(self.SYM, 1100, 2))
        expected = Decimal("100") * Decimal(str(self.MULT))
        self.assertEqual(self.trader.account.realized_pnl, expected)

    def test_weighted_average_open_price(self):
        """分笔开仓后均价按手数加权。"""
        self._open(1000, 1, 1)
        self._open(1100, 1, 2)
        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.volume, 2)
        self.assertEqual(pos.open_price_avg, Decimal("1050.00"))

    def test_average_price_unchanged_by_partial_close(self):
        """部分平仓不应改变剩余持仓的均价。"""
        self._open(1000, 1, 1)
        self._open(1100, 1, 2)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1200"), volume=1))
        self.trader.on_tick(make_tick(self.SYM, 1200, 3))
        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.volume, 1)
        self.assertEqual(pos.open_price_avg, Decimal("1050.00"))

    def test_margin_fully_released_after_close(self):
        self._open(1000, 2, 1)
        self.assertGreater(self.trader.account.margin, 0)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1000"), volume=2))
        self.trader.on_tick(make_tick(self.SYM, 1000, 2))
        self.assertEqual(self.trader.account.margin, Decimal("0"))
        self.assertEqual(self.trader.account.frozen_margin, Decimal("0"))
        self.assertEqual(self.trader.get_positions(), [])

    def test_hedge_positions_are_independent(self):
        """同合约双向持仓应独立核算。"""
        self._open(1000, 2, 1)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=3))
        self.trader.on_tick(make_tick(self.SYM, 1000, 2))
        vols = {p.direction.value: p.volume for p in self.trader.get_positions()}
        self.assertEqual(vols, {"BUY": 2, "SELL": 3})


class CloseExceedsOrderLimitTests(unittest.TestCase):
    """回归: 持仓手数大于单笔报单上限时必须仍能平仓。

    实测故障 (2026-09-14): 裸空 FU2611 177 手, 连续 4 次平仓全部被拒 ——
    `单笔手数 177 超过风控上限 50`。原因是单笔手数检查在开/平分支之前
    无条件执行, 平仓单也被当开仓单拦下, 导致"持仓越大越平不掉"的死锁。
    平仓是减仓行为, 不应受开仓用的单笔上限约束。
    """

    def setUp(self):
        self.trader = PaperTrader(Decimal("10000000"))
        self.SYM = "SA2701"        # 乘数小, 便于在资金上限内建大仓位
        self.LIMIT = self.trader.risk_manager.max_order_volume

    def _open(self, price, volume, sec):
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal(str(price)), volume=volume))
        self.trader.on_tick(make_tick(self.SYM, price, sec))

    def test_open_still_blocked_above_limit(self):
        """开仓单仍须受单笔上限约束 (豁免只针对平仓)。"""
        order = self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=self.LIMIT + 1))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("单笔手数", order.reject_reason or "")

    def test_close_single_order_above_limit_passes_risk(self):
        """平仓单即使超过单笔上限, 也应由风控放行 (可用持仓足够)。"""
        # 直接构造持仓, 绕过开仓上限
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=self.LIMIT))
        self.trader.on_tick(make_tick(self.SYM, 1000, 1))
        pos = self.trader.positions[self.trader.get_pos_key(self.SYM, Direction.BUY)]
        pos.volume = self.LIMIT * 4          # 手工放大到 4 倍上限

        order = self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1000"), volume=self.LIMIT * 4))
        self.assertEqual(order.status, OrderStatus.SUBMITTED,
                         f"平仓不应被单笔上限拦截, 实际拒绝理由: {order.reject_reason}")

    def test_close_all_splits_into_batches(self):
        """一键全平应拆单: 超上限的持仓被拆成多笔, 每笔不超过单笔上限。"""
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=self.LIMIT))
        self.trader.on_tick(make_tick(self.SYM, 1000, 1))
        pos = self.trader.positions[self.trader.get_pos_key(self.SYM, Direction.BUY)]
        total = self.LIMIT * 3 + 7
        pos.volume = total

        ids = self.trader.close_all_positions()

        expected_batches = (total + self.LIMIT - 1) // self.LIMIT
        self.assertEqual(len(ids), expected_batches,
                         f"{total} 手应按 {self.LIMIT} 手/笔拆成 {expected_batches} 笔")
        close_orders = [self.trader.orders[i] for i in ids]
        self.assertTrue(all(o.volume <= self.LIMIT for o in close_orders),
                        "任何单笔平仓委托都不应超过单笔上限")
        self.assertEqual(sum(o.volume for o in close_orders), total,
                         "拆单后总手数必须精确等于可平手数, 不得漏平")
        self.assertTrue(all(o.status == OrderStatus.SUBMITTED for o in close_orders))

    def test_close_all_split_does_not_double_freeze(self):
        """拆单过程中冻结量应被逐批释放, 否则第二笔起会报"可用持仓不足"。"""
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=self.LIMIT))
        self.trader.on_tick(make_tick(self.SYM, 1000, 1))
        pos = self.trader.positions[self.trader.get_pos_key(self.SYM, Direction.BUY)]
        pos.volume = self.LIMIT * 2

        ids = self.trader.close_all_positions()
        self.assertEqual(len(ids), 2, "两批都提交成功 (无'超过可用持仓'拒单)")
        for oid in ids:
            self.assertNotIn("超过可用持仓", self.trader.orders[oid].reject_reason or "")

    def test_inflight_close_orders_mark_position_as_being_closed(self):
        """回归误报: 拆单提交后 frozen_volume 会等于全量持仓。

        若 close-all 接口用 `volume - frozen_volume` 判断"未平仓", 每轮正常
        全平都会误报 partial (实测 177 手拆 4 笔全部 SUBMITTED, 却报 150 手未平)。
        正确判据是"是否已有在途 CLOSE 委托"。
        """
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=self.LIMIT))
        self.trader.on_tick(make_tick(self.SYM, 1000, 1))
        pos = self.trader.positions[self.trader.get_pos_key(self.SYM, Direction.BUY)]
        total = self.LIMIT + 10
        pos.volume = total

        ids = self.trader.close_all_positions()
        self.assertEqual(len(ids), 2)

        # 在途 CLOSE 委托的方向集合
        closing_keys = set()
        for oid in ids:
            o = self.trader.orders[oid]
            self.assertEqual(o.status, OrderStatus.SUBMITTED)
            closing_keys.add(f"{o.instrument}_{o.direction.value}")
        pos_after = self.trader.get_positions()[0]
        close_dir = (Direction.SELL if pos_after.direction == Direction.BUY
                     else Direction.BUY)
        self.assertIn(f"{pos_after.instrument}_{close_dir.value}", closing_keys,
                      "已提交平仓委托的持仓不应被判定为'未能平仓'")
        # 拆单时冻结量逐批释放, 提交完毕后残余的是最后一批的手数 (不是全量)。
        # 无论残留多少, 只要 frozen_volume > 0 旧判据就会低估"未平仓"手数,
        # 因此判据必须是"有无在途 CLOSE 委托", 不能是 volume - frozen_volume。
        self.assertGreater(pos_after.frozen_volume, 0,
                           "提交后应有在途冻结, 旧判据在此会误报未平仓")


class ZeroedPositionCleanupTests(unittest.TestCase):
    """持仓归零后必须从字典移除, 不能被后续开仓复用。

    旧实现平仓到 0 手时只把 open_price_avg/margin/floating_pnl 清零, 对象仍留在
    `self.positions` 里。再次同方向开仓时 `positions.get(pos_key)` 会命中这个
    僵尸对象, 于是旧值参与加权 —— 均价被污染 (0 与真实价加权), 保证金从旧基数累加。
    """

    def setUp(self):
        self.trader = PaperTrader(Decimal("1000000"))
        self.SYM = "SA2701"

    def _tick(self, price, sec):
        self.trader.on_tick(make_tick(self.SYM, price, sec))

    def test_zeroed_position_is_removed_from_dict(self):
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=2))
        self._tick(1000, 1)
        key = self.trader.get_pos_key(self.SYM, Direction.BUY)
        self.assertIn(key, self.trader.positions)

        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1000"), volume=2))
        self._tick(1000, 2)

        self.assertNotIn(key, self.trader.positions,
                         "持仓归零后应被移除, 否则会被下次开仓复用并污染均价")

    def test_reopen_after_full_close_has_clean_average(self):
        """全平后重新开仓, 均价必须只反映新开仓价格。"""
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=2))
        self._tick(1000, 1)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1000"), volume=2))
        self._tick(1000, 2)

        # 以完全不同价格重新开仓
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1500"), volume=3))
        self._tick(1500, 3)

        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.volume, 3)
        self.assertEqual(pos.open_price_avg, Decimal("1500.00"),
                         "重开仓均价不应受已平掉的旧仓影响")

    def test_reopen_margin_matches_fresh_position(self):
        """重开仓占用保证金应与全新账户开同样仓位完全一致。"""
        fresh = PaperTrader(Decimal("1000000"))
        fresh.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1500"), volume=3))
        fresh.on_tick(make_tick(self.SYM, 1500, 3))

        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=2))
        self._tick(1000, 1)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1000"), volume=2))
        self._tick(1000, 2)
        self.trader.submit_order(OrderRequest(
            instrument=self.SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1500"), volume=3))
        self._tick(1500, 3)

        self.assertEqual(self.trader.account.margin, fresh.account.margin,
                         "重开仓保证金不应带着已平仓的旧基数")


class CancelFundsTests(unittest.TestCase):
    """挂单-撤单 资金闭环。"""

    def test_frozen_margin_restored_exactly_on_cancel(self):
        trader = PaperTrader(Decimal("1000000"))
        sym = "SA2701"
        before = trader.account.available
        order = trader.submit_order(OrderRequest(
            instrument=sym, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("500"), volume=2))
        self.assertGreater(trader.account.frozen_margin, 0, "挂单应冻结保证金")
        self.assertLess(trader.account.available, before)

        self.assertTrue(trader.cancel_order(order.order_id))
        self.assertEqual(trader.account.frozen_margin, Decimal("0"))
        self.assertEqual(trader.account.available, before, "撤单后可用资金应精确复原")

    def test_double_cancel_returns_false(self):
        trader = PaperTrader(Decimal("1000000"))
        order = trader.submit_order(OrderRequest(
            instrument="SA2701", direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("500"), volume=1))
        self.assertTrue(trader.cancel_order(order.order_id))
        self.assertFalse(trader.cancel_order(order.order_id), "重复撤单应返回 False")


if __name__ == "__main__":
    unittest.main()
