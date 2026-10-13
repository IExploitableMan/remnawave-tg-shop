"""Persist operation messages and the explicitly confirmed upgrade target."""
from alembic import op
import sqlalchemy as sa
revision = '0016_infrastructure_job_messages'
down_revision = '0015_infrastructure_access_only'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('progress_message_id', 'completion_message_id'):
        op.add_column('infrastructure_jobs', sa.Column(name, sa.BigInteger(), nullable=True))
    op.add_column('infrastructure_jobs', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('infrastructure_jobs', sa.Column('target_version', sa.String(32), nullable=True))


def downgrade():
    for name in ('target_version', 'started_at', 'completion_message_id', 'progress_message_id'):
        op.drop_column('infrastructure_jobs', name)
