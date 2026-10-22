"""Add configurable multi-model AI analysis audit tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "20260916_ai_analysis"
down_revision = "20260913_trading_state"
branch_labels = None
depends_on = None

LONG = sa.Text().with_variant(LONGTEXT(), "mysql")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ai_model_config"):
        op.create_table(
            "ai_model_config",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("model_config_id", sa.String(36), nullable=False, unique=True),
            sa.Column("owner_username", sa.String(64), nullable=False),
            sa.Column("display_name", sa.String(64), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("base_url", sa.String(512), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("credential_ref", sa.String(255)),
            sa.Column("requires_api_key", sa.Boolean(), nullable=False),
            sa.Column("is_local_service", sa.Boolean(), nullable=False),
            sa.Column("max_context_tokens", sa.Integer(), nullable=False),
            sa.Column("max_output_tokens", sa.Integer(), nullable=False),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False),
            sa.Column("temperature", sa.Numeric(3, 2), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("last_test_status", sa.String(24)),
            sa.Column("last_test_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("owner_username", "display_name", name="uq_ai_model_owner_name"),
        )
        op.create_index("ix_ai_model_config_model_config_id", "ai_model_config", ["model_config_id"], unique=True)
        op.create_index("ix_ai_model_config_owner_username", "ai_model_config", ["owner_username"])
        op.create_index("ix_ai_model_config_enabled", "ai_model_config", ["enabled"])
    if not inspector.has_table("ai_model_test_log"):
        op.create_table(
            "ai_model_test_log",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("test_id", sa.String(36), nullable=False, unique=True),
            sa.Column("owner_username", sa.String(64), nullable=False),
            sa.Column("model_config_id", sa.String(36), nullable=False),
            sa.Column("request_json", LONG, nullable=False),
            sa.Column("raw_response_text", LONG),
            sa.Column("http_status", sa.Integer()),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("latency_ms", sa.BigInteger()),
            sa.Column("sanitized_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
        )
        op.create_index("ix_ai_model_test_log_test_id", "ai_model_test_log", ["test_id"], unique=True)
        op.create_index("ix_ai_model_test_log_owner_username", "ai_model_test_log", ["owner_username"])
        op.create_index("ix_ai_model_test_log_model_config_id", "ai_model_test_log", ["model_config_id"])
    if not inspector.has_table("ai_analysis_batch"):
        op.create_table(
            "ai_analysis_batch",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", sa.String(36), nullable=False, unique=True),
            sa.Column("client_request_id", sa.String(36), nullable=False),
            sa.Column("owner_username", sa.String(64), nullable=False),
            sa.Column("instrument_id", sa.String(32), nullable=False),
            sa.Column("instrument_name", sa.String(64), nullable=False),
            sa.Column("feed", sa.String(16), nullable=False),
            sa.Column("trading_day", sa.String(16), nullable=False),
            sa.Column("snapshot_time", sa.DateTime(), nullable=False),
            sa.Column("snapshot_hash", sa.String(64), nullable=False),
            sa.Column("snapshot_json", LONG, nullable=False),
            sa.Column("selected_models_json", LONG, nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
            sa.UniqueConstraint("owner_username", "client_request_id", name="uq_ai_batch_owner_request"),
        )
        op.create_index("ix_ai_analysis_batch_batch_id", "ai_analysis_batch", ["batch_id"], unique=True)
        op.create_index("ix_ai_analysis_batch_owner_username", "ai_analysis_batch", ["owner_username"])
        op.create_index("ix_ai_analysis_batch_instrument_id", "ai_analysis_batch", ["instrument_id"])
        op.create_index("ix_ai_analysis_batch_trading_day", "ai_analysis_batch", ["trading_day"])
        op.create_index("ix_ai_analysis_batch_status", "ai_analysis_batch", ["status"])
        op.create_index("ix_ai_batch_owner_created", "ai_analysis_batch", ["owner_username", "created_at"])
    if not inspector.has_table("ai_analysis_run"):
        op.create_table(
            "ai_analysis_run",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(36), nullable=False, unique=True),
            sa.Column("batch_id", sa.String(36), nullable=False),
            sa.Column("model_config_id", sa.String(36), nullable=False),
            sa.Column("model_display_name", sa.String(64), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("base_url_snapshot", sa.String(512), nullable=False),
            sa.Column("config_json", LONG, nullable=False),
            sa.Column("config_hash", sa.String(64), nullable=False),
            sa.Column("execution_mode", sa.String(16)),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("parsed_response_json", LONG),
            sa.Column("direction", sa.String(16)),
            sa.Column("confidence_score", sa.Integer()),
            sa.Column("call_count", sa.Integer(), nullable=False),
            sa.Column("input_tokens", sa.BigInteger()),
            sa.Column("output_tokens", sa.BigInteger()),
            sa.Column("latency_ms", sa.BigInteger()),
            sa.Column("sanitized_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime()),
            sa.Column("completed_at", sa.DateTime()),
            sa.UniqueConstraint("batch_id", "model_config_id", name="uq_ai_run_batch_model"),
        )
        op.create_index("ix_ai_analysis_run_run_id", "ai_analysis_run", ["run_id"], unique=True)
        op.create_index("ix_ai_analysis_run_batch_id", "ai_analysis_run", ["batch_id"])
        op.create_index("ix_ai_analysis_run_model_config_id", "ai_analysis_run", ["model_config_id"])
        op.create_index("ix_ai_analysis_run_status", "ai_analysis_run", ["status"])
        op.create_index("ix_ai_analysis_run_direction", "ai_analysis_run", ["direction"])
    if not inspector.has_table("ai_analysis_call"):
        op.create_table(
            "ai_analysis_call",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("call_id", sa.String(36), nullable=False, unique=True),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("sequence_no", sa.Integer(), nullable=False),
            sa.Column("call_type", sa.String(16), nullable=False),
            sa.Column("chunk_start_time", sa.DateTime()),
            sa.Column("chunk_end_time", sa.DateTime()),
            sa.Column("request_json", LONG, nullable=False),
            sa.Column("raw_response_text", LONG),
            sa.Column("http_status", sa.Integer()),
            sa.Column("provider_request_id", sa.String(128)),
            sa.Column("input_tokens", sa.BigInteger()),
            sa.Column("output_tokens", sa.BigInteger()),
            sa.Column("latency_ms", sa.BigInteger()),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("sanitized_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
            sa.UniqueConstraint("run_id", "sequence_no", name="uq_ai_call_run_sequence"),
        )
        op.create_index("ix_ai_analysis_call_call_id", "ai_analysis_call", ["call_id"], unique=True)
        op.create_index("ix_ai_analysis_call_run_id", "ai_analysis_call", ["run_id"])


def downgrade():
    for table in ("ai_analysis_call", "ai_analysis_run", "ai_analysis_batch", "ai_model_test_log", "ai_model_config"):
        if sa.inspect(op.get_bind()).has_table(table):
            op.drop_table(table)
