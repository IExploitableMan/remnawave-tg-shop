"""Allow durable queued jobs while preventing concurrent execution per node."""
from alembic import op
import sqlalchemy as sa

revision = '0014_infrastructure_job_queue'
down_revision = '0013_infra_unknown_deadline'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('uq_infrastructure_active_job', table_name='infrastructure_jobs')
    op.create_index('uq_infrastructure_active_job', 'infrastructure_jobs', ['node_id'],
                    unique=True, postgresql_where=sa.text("status = 'running'"))


def downgrade():
    # Refuse a lossy downgrade: queued jobs must be completed/cancelled first
    # if multiple jobs still exist for the same node.
    op.drop_index('uq_infrastructure_active_job', table_name='infrastructure_jobs')
    op.create_index('uq_infrastructure_active_job', 'infrastructure_jobs', ['node_id'],
                    unique=True, postgresql_where=sa.text("status IN ('pending', 'running')"))
