"""Carry operation parameters, such as the bridge SNI, inside the job row."""
from alembic import op
import sqlalchemy as sa
revision = '0018_infrastructure_job_payload'
down_revision = '0017_infra_active_node_names'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('infrastructure_jobs', sa.Column('payload', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('infrastructure_jobs', 'payload')
