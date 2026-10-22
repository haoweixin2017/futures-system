from urllib.parse import quote_plus
from typing import Optional
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from backend.common.config import cfg
from backend.models.database import Base


def _build_url(database: Optional[str] = None) -> str:
    encoded_password = quote_plus(cfg.mysql.password)
    db = database or cfg.mysql.database
    return (
        f"mysql+pymysql://{cfg.mysql.user}:{encoded_password}@"
        f"{cfg.mysql.host}:{cfg.mysql.port}/{db}?charset=utf8mb4"
    )


engine = create_engine(
    _build_url(),
    pool_size=cfg.mysql.pool_size,
    pool_recycle=cfg.mysql.pool_recycle,
    echo=False
)


class _SessionFactory:
    """可重绑定数据库的会话工厂: 每次调用绑定当前模块级 engine (测试隔离用)"""

    def __call__(self, *args, **kwargs):
        return Session(*args, bind=engine, autocommit=False, autoflush=False, **kwargs)


SessionLocal = _SessionFactory()


def rebind_engine(database: str, create: bool = True):
    """重新绑定数据库连接池 (单元测试切换隔离库时使用)"""
    global engine
    if create:
        import pymysql
        conn = pymysql.connect(
            host=cfg.mysql.host,
            port=cfg.mysql.port,
            user=cfg.mysql.user,
            password=cfg.mysql.password,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4")
            conn.commit()
        finally:
            conn.close()
    old_engine = engine
    engine = create_engine(
        _build_url(database),
        pool_size=cfg.mysql.pool_size,
        pool_recycle=cfg.mysql.pool_recycle,
        echo=False
    )
    old_engine.dispose()


def _ensure_columns():
    """轻量迁移: create_all 只建新表, 不给已存在的表补新列; 这里按清单幂等补齐。"""
    wanted = {
        "market_bar": {
            "provenance_json": "TEXT NULL",
        },
        "ai_analysis_follow_up": {
            "attach_latest": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "ai_research_settings": {
            "ai_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        },
    }
    with engine.connect() as conn:
        for table, columns in wanted.items():
            existing = {
                row[0] for row in conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
                ), {"table": table})
            }
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {ddl}"))
        conn.commit()


def init_db():
    # 新增研究模拟表同样由 create_all 幂等创建，不改现有交易表。
    from backend.ai_analysis import paper_verification_models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_verification_window_index()


def _ensure_verification_window_index():
    """兼容旧账本：每个分析时间唯一，同一小节可容纳多次分析。"""
    with engine.begin() as conn:
        indexes = {row[0] for row in conn.execute(text(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_paper_verification_trial'"))}
        if 'uq_verification_plan_due' not in indexes:
            conn.execute(text('ALTER TABLE ai_paper_verification_trial ADD UNIQUE INDEX '
                              'uq_verification_plan_due (plan_id, analysis_due_at)'))
        if 'uq_verification_plan_session' in indexes:
            conn.execute(text('ALTER TABLE ai_paper_verification_trial '
                              'DROP INDEX uq_verification_plan_session'))
