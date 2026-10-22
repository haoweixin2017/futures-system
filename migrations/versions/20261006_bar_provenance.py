"""Nullable factual source metadata; legacy rows remain unknown."""
from alembic import op
import sqlalchemy as sa

revision = "20261006_bar_provenance"
down_revision = "20261002_hybrid_research"
branch_labels = None
depends_on = None


def upgrade():
    if "provenance_json" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("market_bar")}:
        op.add_column("market_bar", sa.Column("provenance_json", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("market_bar", "provenance_json")
