"""Hybrid research: AI assistance becomes optional for scheduled collection."""
import sqlalchemy
from alembic import op

revision = "20261002_hybrid_research"
down_revision = "20261002_scheduled_research"
branch_labels = None
depends_on = None


def _has_column(bind, table, column):
    query = ("SELECT 1 FROM information_schema.COLUMNS "
             "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND COLUMN_NAME = :column")
    return bind.execute(sqlalchemy.text(query), {"table": table, "column": column}).first() is not None


def upgrade():
    bind = op.get_bind()
    # create_all created the research tables on databases that never ran alembic;
    # both fresh and upgraded databases must reach the same column set idempotently.
    if not _has_column(bind, "ai_research_settings", "ai_enabled"):
        op.add_column("ai_research_settings",
                      sqlalchemy.Column("ai_enabled", sqlalchemy.Boolean, nullable=False,
                                        server_default=sqlalchemy.false()))


def downgrade():
    bind = op.get_bind()
    if _has_column(bind, "ai_research_settings", "ai_enabled"):
        op.drop_column("ai_research_settings", "ai_enabled")
