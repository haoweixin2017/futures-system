from enum import Enum
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field
from backend.trade_engine.models import Direction, OffsetFlag

class StopOrderType(str, Enum):
    STOP_LOSS = "STOP_LOSS"       # 止损单
    TAKE_PROFIT = "TAKE_PROFIT"   # 止盈单

class StopOrderStatus(str, Enum):
    ACTIVE = "ACTIVE"             # 监控中
    TRIGGERED = "TRIGGERED"       # 已触发委托
    CANCELLED = "CANCELLED"       # 已撤销

class StopOrderRequest(BaseModel):
    instrument: str
    direction: Direction           # 平多是 SELL, 平空是 BUY
    offset: OffsetFlag = OffsetFlag.CLOSE
    trigger_price: Decimal = Field(gt=0, allow_inf_nan=False) # 触发价
    compare_op: Literal[">=", "<="]
    volume: int = Field(gt=0, strict=True)
    order_price: Optional[Decimal] = Field(default=None, gt=0, allow_inf_nan=False) # 成交委托价，若为 None 则按触发时最新价撮合

class StopOrderData(BaseModel):
    stop_id: str
    instrument: str
    direction: Direction
    offset: OffsetFlag
    trigger_price: Decimal
    compare_op: str
    volume: int
    order_price: Optional[Decimal] = None
    status: StopOrderStatus = StopOrderStatus.ACTIVE
    created_time: datetime = Field(default_factory=datetime.utcnow)
    triggered_time: Optional[datetime] = None
    triggered_order_id: Optional[str] = None
    parent_order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_id": self.stop_id,
            "instrument": self.instrument,
            "direction": self.direction.value,
            "offset": self.offset.value,
            "trigger_price": str(self.trigger_price),
            "compare_op": self.compare_op,
            "volume": self.volume,
            "order_price": str(self.order_price) if self.order_price else "MARKET",
            "status": self.status.value,
            "created_time": self.created_time.isoformat(),
            "triggered_time": self.triggered_time.isoformat() if self.triggered_time else None,
            "triggered_order_id": self.triggered_order_id or ""
        }
