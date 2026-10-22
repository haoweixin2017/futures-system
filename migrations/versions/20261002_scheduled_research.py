"""User-scoped scheduled public research, jobs and immutable source versions."""
from alembic import op
from backend.models.database import AIResearchSettingsModel, AIResearchJobModel, AIResearchMaterialModel

revision = "20261002_scheduled_research"
down_revision = "20260917_ai_review_summary"
branch_labels = None
depends_on = None


def upgrade():
    for model in (AIResearchSettingsModel, AIResearchJobModel, AIResearchMaterialModel):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (AIResearchMaterialModel, AIResearchJobModel, AIResearchSettingsModel):
        model.__table__.drop(op.get_bind(), checkfirst=True)
