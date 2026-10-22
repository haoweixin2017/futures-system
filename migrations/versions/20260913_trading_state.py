"""Persist isolated paper accounts and CTP reconciliation state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "20260913_trading_state"
down_revision = "f66c07478ffc"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("trading_state"):
        op.create_table("trading_state",
                        sa.Column("state_key", sa.String(191), primary_key=True),
                        sa.Column("payload", sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
                        sa.Column("version", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("trading_state")
