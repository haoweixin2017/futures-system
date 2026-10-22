"""Wire validation and errors; unknown market values remain None."""
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, TypedDict, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

BEIJING = timezone(timedelta(hours=8))

# Output wire objects are deliberately separate from ORM/BarData. The reader and
# service create these white-listed shapes; None is not coerced to market zero.
class BarObservation(TypedDict):
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    open_interest: Optional[Decimal]
    turnover: Optional[Decimal]


class FrozenReference(TypedDict):
    run_id: str
    instrument: str
    source: str
    source_timeframe: str
    completed_at: datetime
    valid_from: datetime
    valid_until: datetime
    direction: Literal['LONG', 'SHORT']
    target: Decimal
    stop: Decimal
    explicit_selection: Literal[True]


class RewardRisk(TypedDict):
    status: Literal['AVAILABLE', 'UNAVAILABLE']
    ratio: Optional[Decimal]
    reason_code: Optional[str]
    gross: Literal[True]
    fees_included: Literal[False]


class FrozenSnapshot(TypedDict):
    instrument: str
    feed: Literal['sina']
    timeframe: Literal['15m']
    trading_day: str
    frozen_at: datetime
    quote_at: datetime
    observed_bar: BarObservation
    history_15m: list[dict[str, Any]]
    history_60m: list[dict[str, Any]]
    reference_plan: Optional[FrozenReference]


class Comparison(TypedDict):
    snapshot_id: str
    client_sequence: int
    current: dict[str, Any]
    simulated: dict[str, Any]
    changes: list[dict[str, Any]]
    score_changes: dict[str, Optional[int]]


class ErrorContext(TypedDict):
    stage: Literal['CREATE', 'SIMULATE', 'RELEASE']
    instrument: Optional[str]
    snapshot_id: Optional[str]
    frozen_at: Optional[datetime]
    inputs: Optional[dict[str, Any]]


def now_beijing():
    return datetime.now(BEIJING)


def at(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def wire(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return at(value).isoformat()
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    return value


class SimulatorError(Exception):
    def __init__(self, code, message, status=422, *, reasons=None, fields=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.reasons, self.fields = reasons or [], fields or {}

    def detail(self, context=None):
        return wire(dict(code=self.code, message=self.message, reasons=self.reasons,
                         fields=self.fields, context=context))


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instrument: str = Field(min_length=2, max_length=16, pattern=r'^[A-Za-z]{1,3}[0-9]{3,4}$')
    reference_run_id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class ScenarioInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str = Field(min_length=1, max_length=128)
    client_sequence: StrictInt = Field(default=0, ge=0, le=2147483647)
    use_defaults: bool = Field(default=False, strict=True)
    simulate_price: Optional[str] = None
    volume_ratio: Optional[str] = None
    open_interest_change: Optional[StrictInt] = None

    @field_validator('simulate_price', 'volume_ratio', mode='before')
    @classmethod
    def finite_decimal(cls, v, info):
        if v is None:
            return v
        if not isinstance(v, str) or len(v) > 32 or not re.fullmatch(r'\d+(?:\.\d{1,12})?', v):
            raise ValueError('请输入最多32字符、12位小数的有限十进制字符串')
        n = Decimal(v)
        if not n.is_finite() or (info.field_name == 'simulate_price' and n <= 0):
            raise ValueError('价格必须为正，量比必须非负')
        return v

    @field_validator('open_interest_change')
    @classmethod
    def finite_integer(cls, v):
        if v is not None and len(str(v)) > 32:
            raise ValueError('持仓变化过大')
        return v

    @model_validator(mode='after')
    def modes(self):
        fields = {'simulate_price', 'volume_ratio', 'open_interest_change'}
        if self.use_defaults:
            if fields & self.model_fields_set:
                raise ValueError('默认模式不能携带覆盖字段')
        elif any(getattr(self, k) is None for k in fields):
            raise ValueError('覆盖模式必须填写价格、量比及整数持仓变化')
        return self
