try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag, OrderStatus
from backend.trade_engine.paper_trader import PaperTrader
from backend.models.market_data import TickData

class TestTradeEngine(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(initial_capital=Decimal('100000.0'))

    def test_open_order_margin_freeze(self):
        # JM2701 乘数60，单价1200，2手，12%保证金 = 1200 * 2 * 60 * 0.12 = 17280
        req = OrderRequest(
            instrument='JM2701',
            direction=Direction.BUY,
            offset=OffsetFlag.OPEN,
            price=Decimal('1200'),
            volume=2
        )
        order = self.trader.submit_order(req)
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.assertEqual(self.trader.account.frozen_margin, Decimal('17280.00'))
        self.assertEqual(self.trader.account.available, Decimal('82720.00'))

    def test_risk_rejections(self):
        # 1. 超过单笔50手
        req_over_vol = OrderRequest(
            instrument='JM2701',
            direction=Direction.BUY,
            offset=OffsetFlag.OPEN,
            price=Decimal('1200'),
            volume=51
        )
        order = self.trader.submit_order(req_over_vol)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn('超过风控上限', order.reject_reason)

        # 2. 资金不足
        req_no_cash = OrderRequest(
            instrument='JM2701',
            direction=Direction.BUY,
            offset=OffsetFlag.OPEN,
            price=Decimal('2000'),
            volume=40 # 2000 * 40 * 60 * 0.12 = 576000 > 100000
        )
        order2 = self.trader.submit_order(req_no_cash)
        self.assertEqual(order2.status, OrderStatus.REJECTED)
        self.assertIn('可用资金不足', order2.reject_reason)

        # 3. 自成交拦截 (先挂买单 1200，再挂卖单 1190)
        req_buy = OrderRequest(
            instrument='JM2701',
            direction=Direction.BUY,
            offset=OffsetFlag.OPEN,
            price=Decimal('1200'),
            volume=1
        )
        self.trader.submit_order(req_buy)
        req_sell = OrderRequest(
            instrument='JM2701',
            direction=Direction.SELL,
            offset=OffsetFlag.OPEN,
            price=Decimal('1190'),
            volume=1
        )
        order_cross = self.trader.submit_order(req_sell)
        self.assertEqual(order_cross.status, OrderStatus.REJECTED)
        self.assertIn('触发自成交风控拦截', order_cross.reject_reason)

    def test_order_fill_and_pnl(self):
        # 提交买单 1200，2手
        req = OrderRequest(
            instrument='JM2701',
            direction=Direction.BUY,
            offset=OffsetFlag.OPEN,
            price=Decimal('1200'),
            volume=2
        )
        order = self.trader.submit_order(req)
        self.assertEqual(order.status, OrderStatus.SUBMITTED)

        # 收到 Tick 价格 1195 <= 1200 撮合成交
        tick1 = TickData(
            instrument='JM2701',
            exchange='DCE',
            trading_day='20260911',
            timestamp=datetime(2026, 9, 11, 9, 30, 0),
            last_price=Decimal('1195'),
            volume=100,
            turnover=Decimal('119500'),
            open_interest=Decimal('10000'),
            bid_price_1=Decimal('1194'),
            bid_volume_1=10,
            ask_price_1=Decimal('1195'),
            ask_volume_1=10
        )
        self.trader.on_tick(tick1)

        # 检查订单状态与持仓
        self.assertEqual(order.status, OrderStatus.FILLED)
        pos = self.trader.positions.get('JM2701_BUY')
        self.assertIsNotNone(pos)
        self.assertEqual(pos.volume, 2)
        self.assertEqual(pos.open_price_avg, Decimal('1195'))
        # 此时持仓均价1195，当前价1195，浮动盈亏为 0
        self.assertEqual(pos.floating_pnl, Decimal('0'))

        # 行情上涨到 1210 (+15点，每点60元，2手 = 15 * 60 * 2 = 1800 浮盈)
        tick2 = TickData(
            instrument='JM2701',
            exchange='DCE',
            trading_day='20260911',
            timestamp=datetime(2026, 9, 11, 9, 35, 0),
            last_price=Decimal('1210'),
            volume=200,
            turnover=Decimal('242000'),
            open_interest=Decimal('10000'),
            bid_price_1=Decimal('1210'),
            bid_volume_1=10,
            ask_price_1=Decimal('1211'),
            ask_volume_1=10
        )
        self.trader.on_tick(tick2)
        self.assertEqual(pos.floating_pnl, Decimal('1800'))
        self.assertEqual(self.trader.account.floating_pnl, Decimal('1800'))

        # 卖平 2手 委托价 1205
        close_req = OrderRequest(
            instrument='JM2701',
            direction=Direction.SELL,
            offset=OffsetFlag.CLOSE,
            price=Decimal('1205'),
            volume=2
        )
        close_order = self.trader.submit_order(close_req)
        # 下一笔行情才驱动新委托撮合，重复快照不得重新成交。
        self.trader.on_tick(tick2.model_copy(update={"timestamp": tick2.timestamp + timedelta(seconds=1)}))
        self.assertEqual(close_order.status, OrderStatus.FILLED)
        self.assertEqual(pos.volume, 0)
        # 平仓价 1210，开仓价 1195，盈利 15点 * 120 = 1800 元
        self.assertEqual(self.trader.account.realized_pnl, Decimal('1800.00'))
        self.assertEqual(self.trader.account.margin, Decimal('0.00'))

if __name__ == '__main__':
    unittest.main()
