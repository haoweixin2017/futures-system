from datetime import datetime
import json
from decimal import Decimal
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


def decode_bar_provenance(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return dict(value) if isinstance(value, dict) else None

class TickData(BaseModel):
    instrument: str
    exchange: str
    trading_day: str
    timestamp: datetime
    last_price: Decimal
    bid_price_1: Optional[Decimal] = None
    bid_volume_1: Optional[int] = 0
    ask_price_1: Optional[Decimal] = None
    ask_volume_1: Optional[int] = 0
    volume: int
    turnover: Decimal
    open_interest: Decimal
    prev_settle: Optional[Decimal] = None
    day_open: Optional[Decimal] = None
    day_high: Optional[Decimal] = None
    day_low: Optional[Decimal] = None
    # 源端权威当日均价 (分时均线, VWAP)。新浪 hq 接口字段 27 直接给出,
    # 与网页「均」值一致; 为 None 时下游回退到自算 VWAP。
    avg_price: Optional[Decimal] = None

    def to_redis_dict(self) -> Dict[str, Any]:
        return {
            "day_open": str(self.day_open) if self.day_open is not None else "",
            "day_high": str(self.day_high) if self.day_high is not None else "",
            "day_low": str(self.day_low) if self.day_low is not None else "",
            "avg_price": str(self.avg_price) if self.avg_price is not None else "",
            "instrument": self.instrument,
            "exchange": self.exchange,
            "trading_day": self.trading_day,
            "timestamp": self.timestamp.isoformat(),
            "last_price": str(self.last_price),
            "bid_price_1": str(self.bid_price_1) if self.bid_price_1 is not None else "",
            "bid_volume_1": self.bid_volume_1 or 0,
            "ask_price_1": str(self.ask_price_1) if self.ask_price_1 is not None else "",
            "ask_volume_1": self.ask_volume_1 or 0,
            "volume": self.volume,
            "turnover": str(self.turnover),
            "open_interest": str(self.open_interest),
            "prev_settle": str(self.prev_settle) if self.prev_settle is not None else "",
        }

class BarData(BaseModel):
    instrument: str
    trading_day: str
    period: str  # 1m, 5m, 15m, 30m, 60m
    bar_time: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    turnover: Decimal
    open_interest: Decimal
    open_interest_change: Decimal = Decimal("0.0")
    provenance_json: Optional[Dict[str, Any]] = None

    def to_redis_dict(self) -> Dict[str, Any]:
        return {
            "instrument": self.instrument,
            "trading_day": self.trading_day,
            "period": self.period,
            "bar_time": self.bar_time.isoformat(),
            "open_price": str(self.open_price),
            "high_price": str(self.high_price),
            "low_price": str(self.low_price),
            "close_price": str(self.close_price),
            "volume": self.volume,
            "turnover": str(self.turnover),
            "open_interest": str(self.open_interest),
            "open_interest_change": str(self.open_interest_change),
            "provenance_json": self.provenance_json,
        }

class SignalReason(BaseModel):
    rule: str
    current: Any
    threshold: Optional[Any] = None
    ratio: Optional[float] = None
    passed: bool

class SignalData(BaseModel):
    instrument: str
    signal: str
    timeframe: str
    rule_score: int
    trigger_price: Decimal
    invalid_price: Optional[Decimal] = None
    reasons: List[SignalReason]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
