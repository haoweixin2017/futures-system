"""独立研究模拟账本；不与交易账户表关联。业务窗口为北京时间，created_at 为 UTC。"""
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, BigInteger, UniqueConstraint, Index
from sqlalchemy.dialects.mysql import LONGTEXT
from backend.models.database import Base


class VerificationPlan(Base):
    __tablename__ = 'ai_paper_verification_plan'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    plan_id = Column(String(36), unique=True, nullable=False, index=True)
    owner_username = Column(String(64), nullable=False, index=True)
    instrument = Column(String(32), nullable=False)
    model_config_id = Column(String(36), nullable=False)
    model_display_name = Column(String(64), nullable=False)
    model_config_hash = Column(String(64), nullable=False)
    config_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    start_day = Column(String(8), nullable=False)
    end_day = Column(String(8), nullable=False)
    status = Column(String(24), nullable=False, default='RUNNING')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    stopped_at = Column(DateTime)
    __table_args__ = (Index('ix_verification_plan_owner_status', 'owner_username', 'status'),)


class VerificationTrial(Base):
    __tablename__ = 'ai_paper_verification_trial'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trial_id = Column(String(36), unique=True, nullable=False, index=True)
    plan_id = Column(String(36), nullable=False, index=True)
    trading_day = Column(String(8), nullable=False)
    session_label = Column(String(32), nullable=False)
    session_start = Column(DateTime, nullable=False)
    session_end = Column(DateTime, nullable=False)
    analysis_due_at = Column(DateTime, nullable=False)
    batch_id = Column(String(36), index=True)
    run_id = Column(String(36))
    status = Column(String(24), nullable=False)
    reason = Column(String(512), nullable=False, default='')
    result_available_at = Column(DateTime)
    state_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False, default='{}')
    # 模型费用与未知用量标志随 state_json 保存；状态和业务时间用独立列方便恢复扫描。
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('plan_id', 'analysis_due_at', name='uq_verification_plan_due'),)
