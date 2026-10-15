"""Allow SSH-only adoption of nodes whose billing deadline is unknown."""
from alembic import op
import sqlalchemy as sa

revision = '0013_infra_unknown_deadline'
down_revision = '0012_infrastructure'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('infrastructure_deployments', 'paid_until',
                    existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade():
    # Unknown deadlines must be resolved explicitly before a downgrade.
    op.alter_column('infrastructure_deployments', 'paid_until',
                    existing_type=sa.DateTime(timezone=True), nullable=False)
