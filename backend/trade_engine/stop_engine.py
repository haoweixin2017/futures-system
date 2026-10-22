import logging
import uuid
import threading
from backend.trade_engine.state_store import atomic
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional, Any
from backend.trade_engine.conditional_models import (
    StopOrderRequest, StopOrderData, StopOrderStatus
)
from backend.trade_engine.models import OrderRequest
from backend.trade_engine.paper_trader import paper_trader
from backend.models.market_data import TickData
from backend.notifier.notifier import notifier

logger = logging.getLogger(__name__)

class StopOrderEngine:
    """
    云端自动止损止盈单与条件委托触发引擎
    1. 实时监听 Tick 数据流
    2. 当价格满足条件 (>= 或 <= 触发价) 时，自动将条件单转为实盘/模拟报单委托
    3. 支持撤销、状态追踪与前台查询
    """
    def __init__(self, trader=None):
        self.trader = trader or paper_trader
        self._lock = threading.RLock()
        self._transaction = None
        self.stop_orders: Dict[str, StopOrderData] = {}

    @atomic
    def submit_stop_order(self, req: StopOrderRequest) -> StopOrderData:
        stop_id = f"STOP_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        data = StopOrderData(
            stop_id=stop_id,
            instrument=req.instrument,
            direction=req.direction,
            offset=req.offset,
            trigger_price=req.trigger_price,
            compare_op=req.compare_op,
            volume=req.volume,
            order_price=req.order_price,
            status=StopOrderStatus.ACTIVE
        )
        self.stop_orders[stop_id] = data
        logger.info(f"Stop order created: {stop_id} {req.instrument} {req.direction.value} {req.volume}手 trigger@{req.compare_op}{req.trigger_price}")
        notifier.notify("STOP_ORDER_SET", f"条件单已设置: {req.instrument} 价格{req.compare_op}{req.trigger_price} 触发{req.direction.value} {req.volume}手", "INFO", "stop_engine")
        return data

    @atomic
    def cancel_stop_order(self, stop_id: str) -> bool:
        order = self.stop_orders.get(stop_id)
        if not order or order.status != StopOrderStatus.ACTIVE:
            return False
        order.status = StopOrderStatus.CANCELLED
        logger.info(f"Stop order cancelled: {stop_id}")
        return True

    @atomic
    def on_tick(self, tick: TickData):
        for stop_id, stop_order in list(self.stop_orders.items()):
            if stop_order.status != StopOrderStatus.ACTIVE:
                continue
            if stop_order.instrument != tick.instrument:
                continue

            if stop_order.parent_order_id:
                parent = self.trader.orders.get(stop_order.parent_order_id)
                if not parent or parent.status in ("CANCELLED", "REJECTED"):
                    stop_order.status = StopOrderStatus.CANCELLED
                    continue
                if parent.status != "FILLED":
                    continue
            is_triggered = False
            if stop_order.compare_op in [">=", ">", "GE"]:
                if tick.last_price >= stop_order.trigger_price:
                    is_triggered = True
            elif stop_order.compare_op in ["<=", "<", "LE"]:
                if tick.last_price <= stop_order.trigger_price:
                    is_triggered = True

            if is_triggered:
                self._trigger_stop_order(stop_order, tick)

    def _trigger_stop_order(self, stop_order: StopOrderData, tick: TickData):
        stop_order.status = StopOrderStatus.TRIGGERED
        stop_order.triggered_time = datetime.utcnow()

        fill_price = stop_order.order_price or tick.last_price
        order_req = OrderRequest(
            instrument=stop_order.instrument,
            direction=stop_order.direction,
            offset=stop_order.offset,
            price=fill_price,
            volume=stop_order.volume
        )
        placed_order = self.trader.submit_order(order_req)
        stop_order.triggered_order_id = placed_order.order_id

        logger.info(f"Stop order triggered: {stop_order.stop_id} -> Generated order {placed_order.order_id}")
        notifier.notify(
            "STOP_ORDER_TRIGGERED",
            f"条件单触发: {stop_order.instrument} 触发价 {stop_order.trigger_price} 当前价 {tick.last_price}，生成委托 {placed_order.order_id}",
            "WARNING",
            "stop_engine"
        )

    def get_stop_orders(self, instrument: Optional[str] = None) -> List[StopOrderData]:
        if instrument:
            return [o for o in self.stop_orders.values() if o.instrument == instrument]
        return list(self.stop_orders.values())

stop_order_engine = StopOrderEngine()
