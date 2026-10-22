from enum import Enum
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator

class Direction(str, Enum):
    BUY = "BUY"      # 买
    SELL = "SELL"    # 卖

class OffsetFlag(str, Enum):
    OPEN = "OPEN"          # 开仓
    CLOSE = "CLOSE"        # 平仓
    CLOSE_TODAY = "CLOSE_TODAY"  # 平今
    CLOSE_YEST = "CLOSE_YEST"    # 平昨

class OrderStatus(str, Enum):
    SUBMITTED = "SUBMITTED"      # 已提交待撮合
    PARTIAL = "PARTIAL"          # 部分成交
    FILLED = "FILLED"            # 全部成交
    CANCELLED = "CANCELLED"      # 已撤单
    REJECTED = "REJECTED"        # 拒单/风控拦截

class OrderRequest(BaseModel):
    instrument: str
    direction: Direction
    offset: OffsetFlag
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    volume: int = Field(gt=0, strict=True)
    order_type: Literal["LIMIT"] = "LIMIT"    # LIMIT / MARKET

    @field_validator("instrument")
    @classmethod
    def canonical_instrument(cls, value):
        import re
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{1,3}[0-9]{3,4}", value):
            raise ValueError("请输入具体期货合约代码")
        return value


class OrderData(BaseModel):
    order_id: str
    instrument: str
    direction: Direction
    offset: OffsetFlag
    price: Decimal
    volume: int
    traded_volume: int = 0
    status: OrderStatus = OrderStatus.SUBMITTED
    created_time: datetime = Field(default_factory=datetime.utcnow)
    reject_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "instrument": self.instrument,
            "direction": self.direction.value,
            "offset": self.offset.value,
            "price": str(self.price),
            "volume": self.volume,
            "traded_volume": self.traded_volume,
            "status": self.status.value,
            "created_time": self.created_time.isoformat(),
            "reject_reason": self.reject_reason or ""
        }

class TradeData(BaseModel):
    trade_id: str
    order_id: str
    instrument: str
    direction: Direction
    offset: OffsetFlag
    price: Decimal
    volume: int
    trade_time: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "instrument": self.instrument,
            "direction": self.direction.value,
            "offset": self.offset.value,
            "price": str(self.price),
            "volume": self.volume,
            "trade_time": self.trade_time.isoformat()
        }

class PositionData(BaseModel):
    instrument: str
    direction: Direction   # BUY: 多头持仓, SELL: 空头持仓
    volume: int = 0        # 总手数
    frozen_volume: int = 0 # 平仓挂单冻结手数
    open_price_avg: Decimal = Decimal("0.0") # 持仓均价
    margin: Decimal = Decimal("0.0")         # 占用保证金
    floating_pnl: Decimal = Decimal("0.0")   # 浮动盈亏

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument": self.instrument,
            "direction": self.direction.value,
            "volume": self.volume,
            "frozen_volume": self.frozen_volume,
            "open_price_avg": str(self.open_price_avg),
            "margin": str(self.margin),
            "floating_pnl": str(self.floating_pnl)
        }

class AccountData(BaseModel):
    account_id: str = "PAPER_DEFAULT"
    balance: Decimal = Decimal("1000000.0")      # 静态权益
    available: Decimal = Decimal("1000000.0")    # 可用资金
    margin: Decimal = Decimal("0.0")             # 占用保证金
    frozen_margin: Decimal = Decimal("0.0")      # 挂单冻结资金
    floating_pnl: Decimal = Decimal("0.0")       # 总浮动盈亏
    realized_pnl: Decimal = Decimal("0.0")       # 平仓累计盈亏

    @property
    def dynamic_equity(self) -> Decimal:
        return self.balance + self.floating_pnl

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "balance": str(self.balance),
            "available": str(self.available),
            "margin": str(self.margin),
            "frozen_margin": str(self.frozen_margin),
            "floating_pnl": str(self.floating_pnl),
            "realized_pnl": str(self.realized_pnl),
            "dynamic_equity": str(self.dynamic_equity)
        }
