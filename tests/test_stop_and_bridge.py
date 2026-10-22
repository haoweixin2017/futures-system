try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from decimal import Decimal
from datetime import datetime
from backend.trade_engine.conditional_models import StopOrderRequest
from backend.trade_engine.stop_engine import StopOrderEngine
from backend.trade_engine.signal_bridge import SignalTradeBridge, auto_config
from backend.trade_engine.paper_trader import paper_trader
from backend.models.market_data import TickData, SignalData, SignalReason

class TestStopAndBridge(unittest.TestCase):
    def test_stop_order_trigger(self):
        engine = StopOrderEngine()
        req = StopOrderRequest(
            instrument='JM2701',
            direction='SELL',
            trigger_price=Decimal('1200'),
            compare_op='<=',
            volume=1
        )
        data = engine.submit_stop_order(req)
        self.assertEqual(data.status, 'ACTIVE')

        # Tick 1205: 未触发
        tick_high = TickData(
            instrument='JM2701',
            exchange='DCE',
            trading_day='20260911',
            timestamp=datetime.utcnow(),
            last_price=Decimal('1205'),
            volume=100, turnover=Decimal('10000'), open_interest=Decimal('1000'),
            bid_price_1=Decimal('1204'), bid_volume_1=10, ask_price_1=Decimal('1205'), ask_volume_1=10
        )
        engine.on_tick(tick_high)
        self.assertEqual(data.status, 'ACTIVE')

        # Tick 1198: <= 1200 触发
        tick_low = TickData(
            instrument='JM2701',
            exchange='DCE',
            trading_day='20260911',
            timestamp=datetime.utcnow(),
            last_price=Decimal('1198'),
            volume=200, turnover=Decimal('20000'), open_interest=Decimal('1000'),
            bid_price_1=Decimal('1197'), bid_volume_1=10, ask_price_1=Decimal('1198'), ask_volume_1=10
        )
        engine.on_tick(tick_low)
        self.assertEqual(data.status, 'TRIGGERED')
        self.assertIsNotNone(data.triggered_order_id)

    def test_signal_to_trade_bridge(self):
        # 开启自动跟单
        auto_config.enabled = True
        auto_config.min_score = 70
        auto_config.cooldown_seconds = 0
        bridge = SignalTradeBridge()

        sig = SignalData(
            instrument='JM2701',
            signal='LONG_WATCH',
            timeframe='15m',
            rule_score=85,
            trigger_price=Decimal('1215'),
            invalid_price=Decimal('1195'),
            reasons=[SignalReason(rule='TEST', current='1', passed=True)],
            timestamp=datetime.utcnow()
        )

        initial_orders_cnt = len(paper_trader.get_orders())
        bridge.on_signal_triggered(sig)

        # 检查是否生成委托订单
        orders = paper_trader.get_orders()
        self.assertEqual(len(orders), initial_orders_cnt + 1)
        latest_order = orders[-1]
        self.assertEqual(latest_order.instrument, 'JM2701')
        self.assertEqual(latest_order.direction, 'BUY')
        self.assertEqual(latest_order.price, Decimal('1215'))

        # 关闭自动交易
        auto_config.enabled = False

if __name__ == '__main__':
    unittest.main()
