import logging
import threading
from backend.trade_engine.state_store import atomic
from decimal import Decimal
from typing import Optional, Dict
from backend.models.market_data import SignalData
from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag
from backend.trade_engine.conditional_models import StopOrderRequest
from backend.trade_engine.paper_trader import paper_trader
from backend.trade_engine.stop_engine import stop_order_engine
from backend.notifier.notifier import notifier

logger = logging.getLogger(__name__)

class AutoTradingConfig:
    def __init__(self):
        self.enabled: bool = False             # 自动跟单总开关 (默认关闭，需用户主动开启)
        self.min_score: int = 80               # 自动跟单最低客观规则评分
        self.default_volume: int = 1           # 默认每次跟单开仓手数
        self.auto_stop_loss_enabled: bool = True # 开启自动挂接止损单
        self.cooldown_seconds: int = 300       # 相同合约开仓冷却时间

auto_config = AutoTradingConfig()

class SignalTradeBridge:
    """
    客观规则信号与交易执行连接桥梁 (Signal-to-Trade Bridge)
    1. 接收 SignalEngine 触发的信号 (LONG_WATCH / SHORT_WATCH)
    2. 若系统开启了自动跟单且评分达到要求，自动生成开仓报单
    3. 若信号附带失效止损价 (invalid_price)，联动止损引擎自动布设云端止损单
    """
    def __init__(self, trader=None, stops=None, config=None):
        self.trader = trader or paper_trader
        self.stops = stops or stop_order_engine
        self.config = config or auto_config
        self._lock = threading.RLock()
        self._transaction = None
        self.last_trade_time: Dict[str, float] = {}

    @atomic
    def on_signal_triggered(self, sig: SignalData):
        if not self.config.enabled:
            logger.info(f"信号 {sig.instrument} {sig.signal} 触发，但自动跟单未开启，保持纯客观观察。")
            return

        if sig.rule_score < self.config.min_score:
            logger.info(f"信号评分 {sig.rule_score} 未达到自动跟单门槛 {self.config.min_score}，跳过。")
            return

        import time
        now = time.time()
        last_t = self.last_trade_time.get(sig.instrument, 0)
        if now - last_t < self.config.cooldown_seconds:
            logger.info(f"合约 {sig.instrument} 处于信号开仓冷却期中，跳过。")
            return


        # 确定方向
        if sig.signal == "LONG_WATCH":
            direction = Direction.BUY
            stop_direction = Direction.SELL
            stop_op = "<="
        elif sig.signal == "SHORT_WATCH":
            direction = Direction.SELL
            stop_direction = Direction.BUY
            stop_op = ">="
        else:
            return

        # 1. 提交开仓报单
        order_req = OrderRequest(
            instrument=sig.instrument,
            direction=direction,
            offset=OffsetFlag.OPEN,
            price=sig.trigger_price,
            volume=self.config.default_volume
        )
        logger.info(f"[自动交易] 响应 {sig.signal} 信号，自动报单: {sig.instrument} {direction.value} {order_req.volume}手 @ {sig.trigger_price}")
        order = self.trader.submit_order(order_req)

        if order.status == "REJECTED":
            return
        self.last_trade_time[sig.instrument] = now

        # 2. 联动止损单挂接
        if self.config.auto_stop_loss_enabled and sig.invalid_price:
            stop_req = StopOrderRequest(
                instrument=sig.instrument,
                direction=stop_direction,
                offset=OffsetFlag.CLOSE,
                trigger_price=sig.invalid_price,
                compare_op=stop_op,
                volume=self.config.default_volume
            )
            stop = self.stops.submit_stop_order(stop_req)
            stop.parent_order_id = order.order_id
            logger.info(f"[自动交易] 自动布设保护性止损条件单: 价格 {stop_op} {sig.invalid_price} 触发 {stop_direction.value}")

signal_bridge = SignalTradeBridge()
