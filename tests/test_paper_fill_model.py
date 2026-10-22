"""模拟盘撮合保真度测试。

背景: 旧实现一律以 `tick.last_price` 成交, 导致两处系统性高估——
1. 完全不看买卖价差, 一来一回打平在系统里是零成本 (真实要亏一个价差);
2. 限价单能占"最新价穿越"的便宜 (挂 1000 买、行情跌到 900 就以 900 成交)。

修复后: 买单吃卖一、卖单砸买一, 且成交价不优于自己的委托价。
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import test_bootstrap  # noqa: F401  必须最先导入以完成测试环境隔离

from backend.common.config import cfg
from backend.models.market_data import TickData
from backend.trade_engine.models import Direction, OffsetFlag, OrderRequest
from backend.trade_engine.paper_trader import PaperTrader


def make_tick(sym, last, sec=0, bid=None, ask=None):
    return TickData(
        instrument=sym, exchange="CZCE", trading_day="20260914",
        timestamp=datetime(2026, 9, 14, 10, 0) + timedelta(seconds=sec),
        last_price=Decimal(str(last)),
        bid_price_1=Decimal(str(bid if bid is not None else last)),
        bid_volume_1=10,
        ask_price_1=Decimal(str(ask if ask is not None else last)),
        ask_volume_1=10,
        volume=100, turnover=Decimal("1000"), open_interest=Decimal("1000"),
    )


SYM = "SA2701"


class FillPriceTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(Decimal("1000000"))
        self.mult = self.trader.get_instrument_multiplier(SYM)

    def test_buy_opens_at_ask_not_last(self):
        """买开应以卖一价成交 (真实要付价差)。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 1, bid=999, ask=1001))
        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.open_price_avg, Decimal("1001.00"))

    def test_sell_closes_at_bid_not_last(self):
        """卖平应以买一价成交。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 1, bid=999, ask=1001))

        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("990"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 2, bid=999, ask=1001))
        self.assertEqual(self.trader.trades[-1].price, Decimal("999"))

    def test_round_trip_with_flat_price_shows_spread_loss(self):
        """最新价不变时来回一手应体现价差亏损 (旧实现为 0)。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 1, bid=999, ask=1001))
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("990"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 2, bid=999, ask=1001))
        # 买 1001 卖 999 -> 每手亏 2 点 × 乘数
        self.assertEqual(self.trader.account.realized_pnl,
                         Decimal("-2") * Decimal(str(self.mult)))

    def test_limit_buy_never_fills_better_than_market(self):
        """行情跌穿委托价时不应白拿全部价格改善。

        挂 1000 买, 行情跌到 900/901 -> 成交价应贴近 901 而非 900。
        """
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=1))
        self.trader.on_tick(make_tick(SYM, 900, 1, bid=899, ask=901))
        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.open_price_avg, Decimal("901.00"),
                         "应以对手价 901 成交, 而非 900")

    def test_buy_fill_never_above_own_limit(self):
        """成交价不得优于自己的委托价 (买单成交价不超过委托价)。

        触发仍按最新价穿越 (last <= 买入委托价), 但成交价被委托价封顶:
        挂 1000 买、行情跌到 900/901 时以 901 成交; 若委托价低于对手价,
        则以自己的委托价成交 (相当于"挂在 1000 等成交")。
        """
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=1))
        # last=995 穿过 1000 -> 触发; 卖一 1005 高于委托价 -> 成交价封顶为 1000
        self.trader.on_tick(make_tick(SYM, 995, 1, bid=994, ask=1005))
        pos = self.trader.get_positions()
        self.assertEqual(len(pos), 1, "最新价已穿过委托价, 应成交")
        self.assertEqual(pos[0].open_price_avg, Decimal("1000.00"),
                         "成交价不应高于自己的买入委托价")

    def test_buy_not_triggered_when_price_above_limit(self):
        """最新价未穿过委托价时不应成交。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("990"), volume=1))
        self.trader.on_tick(make_tick(SYM, 995, 1, bid=994, ask=996))
        self.assertEqual(self.trader.get_positions(), [], "最新价 995 > 委托价 990, 不应成交")

    def test_sell_fill_never_below_own_limit(self):
        """卖单成交价不应低于自己的委托价。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 1, bid=1000, ask=1000))
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
            price=Decimal("1005"), volume=1))
        # last=1010 穿过 1005; 买一 1002 低于委托价 -> 成交价抬到 1005
        self.trader.on_tick(make_tick(SYM, 1010, 2, bid=1002, ask=1012))
        self.assertEqual(self.trader.trades[-1].price, Decimal("1005"))

    def test_missing_bid_ask_falls_back_to_last(self):
        """对手价缺失 (如部分行情源) 时退化为最新价, 不应报错。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        t = make_tick(SYM, 1000, 1)
        t.bid_price_1 = None
        t.ask_price_1 = None
        self.trader.on_tick(t)
        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.open_price_avg, Decimal("1000.00"))

    def test_opponent_price_can_be_disabled(self):
        """配置关闭对手价时, 回退为旧行为 (取最新价)。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        with patch.object(cfg.system, "paper_fill_uses_opponent", False):
            self.trader.on_tick(make_tick(SYM, 1000, 1, bid=999, ask=1005))
        pos = self.trader.get_positions()[0]
        self.assertEqual(pos.open_price_avg, Decimal("1000.00"))


class SlippageTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(Decimal("1000000"))

    def test_slippage_applied_in_unfavorable_direction(self):
        """滑点应始终不利于成交方: 买单加价, 卖单减价。"""
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        with patch.object(cfg.system, "paper_slippage_ticks", 2.0):
            self.trader.on_tick(make_tick(SYM, 1000, 1, bid=999, ask=1001))
        pos = self.trader.get_positions()[0]
        # SA2701 最小变动 1 元 -> 901? 不, ask=1001 + 2*1 = 1003
        self.assertEqual(pos.open_price_avg, Decimal("1003.00"))

    def test_zero_slippage_is_default_noop(self):
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1010"), volume=1))
        self.trader.on_tick(make_tick(SYM, 1000, 1, bid=999, ask=1001))
        self.assertEqual(self.trader.get_positions()[0].open_price_avg, Decimal("1001.00"))


class CommissionTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(Decimal("1000000"))

    def test_no_commission_by_default(self):
        self.trader.submit_order(OrderRequest(
            instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
            price=Decimal("1000"), volume=2))
        self.trader.on_tick(make_tick(SYM, 1000, 1))
        self.assertEqual(self.trader.account.balance, Decimal("1000000"))

    def test_commission_deducted_per_lot(self):
        with patch.object(cfg.system, "paper_commission_per_lot", 5.0):
            self.trader.submit_order(OrderRequest(
                instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                price=Decimal("1000"), volume=3))
            self.trader.on_tick(make_tick(SYM, 1000, 1))
        # 3 手 × 5 元 = 15
        self.assertEqual(self.trader.account.balance, Decimal("999985.00"))

    def test_commission_counts_toward_realized_pnl(self):
        with patch.object(cfg.system, "paper_commission_per_lot", 4.0):
            self.trader.submit_order(OrderRequest(
                instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                price=Decimal("1000"), volume=2))
            self.trader.on_tick(make_tick(SYM, 1000, 1))
        self.assertEqual(self.trader.account.realized_pnl, Decimal("-8.00"))

    def test_commission_applied_on_close_too(self):
        with patch.object(cfg.system, "paper_commission_per_lot", 5.0):
            self.trader.submit_order(OrderRequest(
                instrument=SYM, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                price=Decimal("1000"), volume=1))
            self.trader.on_tick(make_tick(SYM, 1000, 1))
            self.trader.submit_order(OrderRequest(
                instrument=SYM, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
                price=Decimal("1000"), volume=1))
            self.trader.on_tick(make_tick(SYM, 1000, 2))
        # 开平各 5 元 = 10
        self.assertEqual(self.trader.account.balance, Decimal("999990.00"))


if __name__ == "__main__":
    unittest.main()
