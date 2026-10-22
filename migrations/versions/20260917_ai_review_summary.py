"""Add user-controlled AI review summaries and usage audit."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT


revision = "20260917_ai_review_summary"
down_revision = "20260916_ai_analysis"
branch_labels = None
depends_on = None

LONG = sa.Text().with_variant(LONGTEXT(), "mysql")


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("ai_analysis_review_summary"):
        op.create_table(
            "ai_analysis_review_summary",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("summary_id", sa.String(36), nullable=False, unique=True),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("evaluation_id", sa.String(36), nullable=False),
            sa.Column("owner_username", sa.String(64), nullable=False),
            sa.Column("source_model_config_id", sa.String(36), nullable=False),
            sa.Column("generator_model_config_id", sa.String(36), nullable=False),
            sa.Column("generator_config_json", LONG, nullable=False),
            sa.Column("instrument_id", sa.String(32), nullable=False),
            sa.Column("product_code", sa.String(16), nullable=False),
            sa.Column("analysis_target", sa.String(24), nullable=False),
            sa.Column("session_key", sa.String(32)),
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("revision_no", sa.Integer(), nullable=False),
            sa.Column("user_guidance", sa.Text()),
            sa.Column("summary_json", LONG),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("request_json", LONG),
            sa.Column("raw_response_text", LONG),
            sa.Column("input_tokens", sa.BigInteger()),
            sa.Column("output_tokens", sa.BigInteger()),
            sa.Column("latency_ms", sa.BigInteger()),
            sa.Column("sanitized_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime()),
            sa.Column("completed_at", sa.DateTime()),
            sa.Column("activated_at", sa.DateTime()),
            sa.Column("disabled_at", sa.DateTime()),
            sa.UniqueConstraint("run_id", "revision_no", name="uq_ai_review_run_revision"),
        )
        op.create_index("ix_ai_analysis_review_summary_summary_id", "ai_analysis_review_summary", ["summary_id"], unique=True)
        for column in ("run_id", "evaluation_id", "owner_username", "source_model_config_id",
                       "generator_model_config_id", "instrument_id", "product_code",
                       "analysis_target", "session_key", "status"):
            op.create_index(f"ix_ai_analysis_review_summary_{column}", "ai_analysis_review_summary", [column])
        op.create_index("ix_ai_review_match", "ai_analysis_review_summary",
                        ["owner_username", "analysis_target", "product_code", "status"])

    if not inspector.has_table("ai_analysis_review_usage"):
        op.create_table(
            "ai_analysis_review_usage",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("summary_id", sa.String(36), nullable=False),
            sa.Column("sequence_no", sa.Integer(), nullable=False),
            sa.Column("summary_hash", sa.String(64), nullable=False),
            sa.Column("summary_snapshot_json", LONG, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("run_id", "summary_id", name="uq_ai_review_usage_run_summary"),
        )
        op.create_index("ix_ai_analysis_review_usage_run_id", "ai_analysis_review_usage", ["run_id"])
        op.create_index("ix_ai_analysis_review_usage_summary_id", "ai_analysis_review_usage", ["summary_id"])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("ai_analysis_review_usage"):
        op.drop_table("ai_analysis_review_usage")
    if inspector.has_table("ai_analysis_review_summary"):
        op.drop_table("ai_analysis_review_summary")
