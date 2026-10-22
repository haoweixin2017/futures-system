from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import (
    Boolean, Column, Integer, String, DateTime, Numeric, BigInteger, Text, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.mysql import LONGTEXT, DATETIME

Base = declarative_base()

class TradingStateModel(Base):
    __tablename__ = "trading_state"

    state_key = Column(String(191), primary_key=True)
    payload = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    version = Column(Integer, nullable=False, default=1)

class InstrumentModel(Base):
    __tablename__ = "instrument"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), unique=True, nullable=False, index=True)
    exchange = Column(String(16), nullable=False)
    name = Column(String(64), nullable=False)
    price_tick = Column(Numeric(18, 6), default=Decimal("1.0"))
    volume_multiple = Column(Integer, default=10)
    created_at = Column(DateTime, default=datetime.utcnow)

class MarketTickModel(Base):
    __tablename__ = "market_tick"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id = Column(String(32), nullable=False, index=True)
    exchange = Column(String(16), nullable=False)
    trading_day = Column(String(16), nullable=False, index=True)
    tick_time = Column(DateTime, nullable=False, index=True)
    last_price = Column(Numeric(18, 6), nullable=False)
    bid_price_1 = Column(Numeric(18, 6), nullable=True)
    bid_volume_1 = Column(Integer, nullable=True)
    ask_price_1 = Column(Numeric(18, 6), nullable=True)
    ask_volume_1 = Column(Integer, nullable=True)
    volume = Column(BigInteger, nullable=False)
    turnover = Column(Numeric(18, 6), nullable=False)
    open_interest = Column(Numeric(18, 6), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_tick_inst_day", "instrument_id", "trading_day"),
        Index("ix_tick_inst_time", "instrument_id", "tick_time"),
    )

class MarketBarModel(Base):
    __tablename__ = "market_bar"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id = Column(String(32), nullable=False)
    trading_day = Column(String(16), nullable=False)
    period = Column(String(8), nullable=False)  # 1m, 5m, 15m, 30m, 60m, 1d
    bar_time = Column(DateTime, nullable=False)
    open_price = Column(Numeric(18, 6), nullable=False)
    high_price = Column(Numeric(18, 6), nullable=False)
    low_price = Column(Numeric(18, 6), nullable=False)
    close_price = Column(Numeric(18, 6), nullable=False)
    volume = Column(BigInteger, nullable=False)
    turnover = Column(Numeric(18, 6), nullable=False)
    open_interest = Column(Numeric(18, 6), nullable=False)
    open_interest_change = Column(Numeric(18, 6), default=Decimal("0.0"))
    provenance_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("instrument_id", "period", "bar_time", name="uq_bar_inst_period_time"),
        Index("ix_bar_query", "instrument_id", "trading_day"),
        Index("ix_bar_lookup", "instrument_id", "period", "bar_time"),
    )

class SignalRecordModel(Base):
    __tablename__ = "signal_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id = Column(String(32), nullable=False, index=True)
    signal_type = Column(String(32), nullable=False)  # e.g. LONG_WATCH
    timeframe = Column(String(8), nullable=False)
    rule_score = Column(Integer, nullable=False)
    trigger_price = Column(Numeric(18, 6), nullable=False)
    invalid_price = Column(Numeric(18, 6), nullable=True)
    reasons_json = Column(Text, nullable=False)  # 包含客观数值追溯依据
    signal_time = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AIModelConfigModel(Base):
    __tablename__ = "ai_model_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    model_config_id = Column(String(36), nullable=False, unique=True, index=True)
    owner_username = Column(String(64), nullable=False, index=True)
    display_name = Column(String(64), nullable=False)
    provider = Column(String(32), nullable=False, default="openai_compatible")
    base_url = Column(String(512), nullable=False)
    model_name = Column(String(128), nullable=False)
    credential_ref = Column(String(255), nullable=True)
    requires_api_key = Column(Boolean, nullable=False, default=True)
    is_local_service = Column(Boolean, nullable=False, default=False)
    max_context_tokens = Column(Integer, nullable=False, default=32768)
    max_output_tokens = Column(Integer, nullable=False, default=2000)
    timeout_seconds = Column(Integer, nullable=False, default=90)
    temperature = Column(Numeric(3, 2), nullable=False, default=Decimal("0.10"))
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    last_test_status = Column(String(24), nullable=True)
    last_test_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_username", "display_name", name="uq_ai_model_owner_name"),
    )


class AIModelTestLogModel(Base):
    __tablename__ = "ai_model_test_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    test_id = Column(String(36), nullable=False, unique=True, index=True)
    owner_username = Column(String(64), nullable=False, index=True)
    model_config_id = Column(String(36), nullable=False, index=True)
    request_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    raw_response_text = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    http_status = Column(Integer, nullable=True)
    status = Column(String(24), nullable=False)
    latency_ms = Column(BigInteger, nullable=True)
    sanitized_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class AIAnalysisBatchModel(Base):
    __tablename__ = "ai_analysis_batch"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id = Column(String(36), nullable=False, unique=True, index=True)
    client_request_id = Column(String(36), nullable=False)
    owner_username = Column(String(64), nullable=False, index=True)
    instrument_id = Column(String(32), nullable=False, index=True)
    instrument_name = Column(String(64), nullable=False)
    feed = Column(String(16), nullable=False, default="sina")
    trading_day = Column(String(16), nullable=False, index=True)
    snapshot_time = Column(DateTime, nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    snapshot_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    selected_models_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    status = Column(String(24), nullable=False, default="CREATED", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_username", "client_request_id", name="uq_ai_batch_owner_request"),
        Index("ix_ai_batch_owner_created", "owner_username", "created_at"),
    )


class AIAnalysisRunModel(Base):
    __tablename__ = "ai_analysis_run"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, unique=True, index=True)
    batch_id = Column(String(36), nullable=False, index=True)
    model_config_id = Column(String(36), nullable=False, index=True)
    model_display_name = Column(String(64), nullable=False)
    provider = Column(String(32), nullable=False)
    model_name = Column(String(128), nullable=False)
    base_url_snapshot = Column(String(512), nullable=False)
    config_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    config_hash = Column(String(64), nullable=False)
    execution_mode = Column(String(16), nullable=True)
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    parsed_response_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    direction = Column(String(16), nullable=True, index=True)
    confidence_score = Column(Integer, nullable=True)
    call_count = Column(Integer, nullable=False, default=0)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    latency_ms = Column(BigInteger, nullable=True)
    sanitized_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("batch_id", "model_config_id", name="uq_ai_run_batch_model"),
    )


class AIAnalysisCallModel(Base):
    __tablename__ = "ai_analysis_call"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    call_id = Column(String(36), nullable=False, unique=True, index=True)
    run_id = Column(String(36), nullable=False, index=True)
    sequence_no = Column(Integer, nullable=False)
    call_type = Column(String(16), nullable=False)
    chunk_start_time = Column(DateTime, nullable=True)
    chunk_end_time = Column(DateTime, nullable=True)
    request_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    raw_response_text = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    http_status = Column(Integer, nullable=True)
    provider_request_id = Column(String(128), nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    latency_ms = Column(BigInteger, nullable=True)
    status = Column(String(24), nullable=False)
    sanitized_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_ai_call_run_sequence"),
    )


class AIAnalysisEvaluationModel(Base):
    __tablename__ = "ai_analysis_evaluation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id = Column(String(36), nullable=False, unique=True, index=True)
    run_id = Column(String(36), nullable=False, index=True)
    owner_username = Column(String(64), nullable=False, index=True)
    analysis_target = Column(String(24), nullable=False, index=True)
    rating = Column(String(24), nullable=False)
    score = Column(Integer, nullable=True)
    comment = Column(Text, nullable=True)
    evaluated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_username", "run_id", name="uq_ai_evaluation_owner_run"),
        Index("ix_ai_evaluation_owner_target", "owner_username", "analysis_target"),
    )


class AIAnalysisFollowUpModel(Base):
    __tablename__ = "ai_analysis_follow_up"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    followup_id = Column(String(36), nullable=False, unique=True, index=True)
    run_id = Column(String(36), nullable=False, index=True)
    owner_username = Column(String(64), nullable=False, index=True)
    sequence_no = Column(Integer, nullable=False)
    question = Column(Text, nullable=False)
    attach_latest = Column(Boolean, nullable=False, default=False)
    answer = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    request_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    raw_response_text = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    latency_ms = Column(BigInteger, nullable=True)
    sanitized_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_ai_followup_run_sequence"),
        Index("ix_ai_followup_owner_created", "owner_username", "created_at"),
    )


class AIAnalysisReviewSummaryModel(Base):
    __tablename__ = "ai_analysis_review_summary"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    summary_id = Column(String(36), nullable=False, unique=True, index=True)
    run_id = Column(String(36), nullable=False, index=True)
    evaluation_id = Column(String(36), nullable=False, index=True)
    owner_username = Column(String(64), nullable=False, index=True)
    source_model_config_id = Column(String(36), nullable=False, index=True)
    generator_model_config_id = Column(String(36), nullable=False, index=True)
    generator_config_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    instrument_id = Column(String(32), nullable=False, index=True)
    product_code = Column(String(16), nullable=False, index=True)
    analysis_target = Column(String(24), nullable=False, index=True)
    session_key = Column(String(32), nullable=True, index=True)
    scope = Column(String(16), nullable=False)
    revision_no = Column(Integer, nullable=False)
    user_guidance = Column(Text, nullable=True)
    summary_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    request_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    raw_response_text = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    latency_ms = Column(BigInteger, nullable=True)
    sanitized_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    activated_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "revision_no", name="uq_ai_review_run_revision"),
        Index("ix_ai_review_match", "owner_username", "analysis_target", "product_code", "status"),
    )


class AIAnalysisReviewUsageModel(Base):
    __tablename__ = "ai_analysis_review_usage"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True)
    summary_id = Column(String(36), nullable=False, index=True)
    sequence_no = Column(Integer, nullable=False)
    summary_hash = Column(String(64), nullable=False)
    summary_snapshot_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "summary_id", name="uq_ai_review_usage_run_summary"),
    )


class AIResearchSettingsModel(Base):
    __tablename__ = "ai_research_settings"

    owner_username = Column(String(64), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    # AI 辅助研究独立于定时采集开关；关闭时固定采集不产生任何模型调用。
    ai_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    model_config_id = Column(String(36), nullable=True)
    interval_minutes = Column(Integer, nullable=False, default=60)
    contracts_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    config_version = Column(Integer, nullable=False, default=1)
    pause_reason = Column(Text, nullable=True)
    paused_model_hash = Column(String(64), nullable=True)
    updated_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False, default=datetime.utcnow)


class AIResearchJobModel(Base):
    __tablename__ = "ai_research_job"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String(36), nullable=False, unique=True)
    owner_username = Column(String(64), nullable=False, index=True)
    topic = Column(String(32), nullable=False)
    # NULL after completion; unique while queued/running, including across processes.
    active_key = Column(String(64), nullable=True, unique=True)
    config_version = Column(Integer, nullable=False)
    config_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    model_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    trigger = Column(String(16), nullable=False)
    status = Column(String(24), nullable=False, default="QUEUED")
    queued_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    started_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    completed_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    next_check_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    lease_until = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    lease_token = Column(String(36), nullable=True)
    published = Column(Boolean, nullable=False, default=False)
    result_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    calls_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False, default="[]")
    error = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_research_job_owner_topic", "owner_username", "topic", "queued_at"),
        Index("ix_research_job_queue", "status", "queued_at"),
    )


class AIResearchMaterialModel(Base):
    __tablename__ = "ai_research_material"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    material_id = Column(String(36), nullable=False, unique=True)
    owner_username = Column(String(64), nullable=False, index=True)
    url = Column(Text, nullable=False)
    url_hash = Column(String(64), nullable=False)
    content_hash = Column(String(64), nullable=False)
    title = Column(String(512), nullable=False)
    body = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    metadata_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    published_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    first_fetched_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    version_fetched_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    original_job_id = Column(String(36), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_username", "url_hash", "content_hash", name="uq_research_material_version"),
        Index("ix_research_material_owner_time", "owner_username", "version_fetched_at"),
    )


class SystemEventModel(Base):
    __tablename__ = "system_event"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_type = Column(String(32), nullable=False, index=True)  # CTP_DISCONNECT, SIGNAL_TRIGGER, SYSTEM_ERROR, INFO
    severity = Column(String(16), nullable=False, default="INFO")  # INFO, WARNING, ERROR, CRITICAL
    source = Column(String(64), nullable=False)
    message = Column(Text, nullable=False)
    details_json = Column(Text, nullable=True)
    event_time = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class AccountSnapshotModel(Base):
    __tablename__ = "account_snapshot"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    account_id = Column(String(32), nullable=False, index=True)
    trading_day = Column(String(16), nullable=False, index=True)
    snapshot_time = Column(DateTime, nullable=False, index=True)
    balance = Column(Numeric(18, 2), nullable=False)
    dynamic_equity = Column(Numeric(18, 2), nullable=False)
    available = Column(Numeric(18, 2), nullable=False)
    margin = Column(Numeric(18, 2), nullable=False)
    floating_pnl = Column(Numeric(18, 2), nullable=False)
    realized_pnl = Column(Numeric(18, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_acc_day_time", "account_id", "trading_day", "snapshot_time"),
    )


# ---------------------------------------------------------------------------
# 实盘结构化流水表
#
# 为什么要单独建表: trading_state 把订单/成交/持仓全塞在一个 JSON 大字段里,
# 导致柜台对账与盈亏归因无法用 SQL 完成 (见 docs/2026-09-15-实盘测试全流程方案.md)。
# 这三张表是对账的地基, 与现有 JSON 存储**双写**: JSON 存储保留不动 (兼容),
# 结构化表用于逐笔核对与聚合查询。
# ---------------------------------------------------------------------------

class OrderRecordModel(Base):
    """实盘委托流水 (逐笔)。幂等键 order_ref = day_front_session_ref。"""
    __tablename__ = "order_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    order_ref = Column(String(64), nullable=False, unique=True, index=True)
    gateway_ref = Column(String(32), nullable=True)     # 柜台 OrderRef
    broker_id = Column(String(16), nullable=True)
    investor_id = Column(String(32), nullable=True)
    trading_day = Column(String(16), nullable=True, index=True)
    front_id = Column(Integer, nullable=True)
    session_id = Column(Integer, nullable=True)
    order_sys_id = Column(String(32), nullable=True)    # 柜台委托编号
    exchange = Column(String(16), nullable=True)
    instrument_id = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=True)        # BUY / SELL
    offset_flag = Column(String(16), nullable=True)     # OPEN/CLOSE/CLOSE_TODAY/CLOSE_YEST
    price = Column(Numeric(18, 6), nullable=True)
    volume = Column(Integer, nullable=True)
    traded_volume = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=True, index=True)
    status_msg = Column(String(255), nullable=True)
    owner = Column(String(64), nullable=True)
    gateway_code = Column(Integer, nullable=True)
    submit_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_order_day_instrument", "trading_day", "instrument_id"),
    )


class TradeRecordModel(Base):
    """实盘成交流水 (逐笔)。幂等键 trade_key = trading_day:exchange:TradeID。"""
    __tablename__ = "trade_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_key = Column(String(96), nullable=False, unique=True, index=True)
    trade_id = Column(String(32), nullable=True)
    order_ref = Column(String(64), nullable=True, index=True)
    order_sys_id = Column(String(32), nullable=True)
    exchange = Column(String(16), nullable=True)
    instrument_id = Column(String(32), nullable=False, index=True)
    direction = Column(String(8), nullable=True)
    offset_flag = Column(String(16), nullable=True)
    price = Column(Numeric(18, 6), nullable=True)
    volume = Column(Integer, nullable=True)
    trade_time = Column(DateTime, nullable=True)
    trading_day = Column(String(16), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_trade_day_instrument", "trading_day", "instrument_id"),
    )


class PositionSnapshotModel(Base):
    """柜台持仓快照 (每次对账落一份, 支持日间归因与今昨仓核对)。"""
    __tablename__ = "position_snapshot"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trading_day = Column(String(16), nullable=False, index=True)
    snapshot_time = Column(DateTime, nullable=False)
    investor_id = Column(String(32), nullable=True)
    instrument_id = Column(String(32), nullable=False)
    posi_direction = Column(String(4), nullable=True)   # 2=多头 3=空头
    position = Column(Integer, nullable=False, default=0)
    today_position = Column(Integer, nullable=False, default=0)
    yd_position = Column(Integer, nullable=False, default=0)
    long_frozen = Column(Integer, nullable=False, default=0)
    short_frozen = Column(Integer, nullable=False, default=0)
    position_cost = Column(Numeric(18, 6), nullable=True)
    use_margin = Column(Numeric(18, 2), nullable=True)
    position_profit = Column(Numeric(18, 2), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pos_day_instrument", "trading_day", "instrument_id"),
    )
