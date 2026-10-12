"""Infrastructure lifecycle, durable jobs and deployment reports."""
from alembic import op
import sqlalchemy as sa

revision = '0012_infrastructure'
down_revision = '0011_panel_user_ids'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('infrastructure_nodes',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('panel_scope', sa.String(64), nullable=False),
        sa.Column('panel_uuid', sa.String(36), nullable=True),
        sa.Column('host_uuid', sa.String(36), nullable=True),
        sa.Column('name', sa.String(30), nullable=False),
        sa.Column('country', sa.String(2), nullable=False),
        sa.Column('provider_uuid', sa.String(36), nullable=False),
        sa.Column('period_days', sa.Integer, nullable=False, default=30),
        sa.Column('billing_uuid', sa.String(36), nullable=True),
        sa.Column('excluded_tg', sa.Boolean, nullable=False, default=False),
        sa.Column('previous_host_access', sa.Text, nullable=True),
        sa.Column('deleted', sa.Boolean, nullable=False, default=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('panel_scope', 'name'),
        sa.UniqueConstraint('panel_scope', 'panel_uuid'),
    )
    op.create_index('ix_infrastructure_nodes_panel_scope', 'infrastructure_nodes', ['panel_scope'])
    op.create_table('infrastructure_deployments',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('node_id', sa.Integer, sa.ForeignKey('infrastructure_nodes.id'), nullable=False),
        sa.Column('address', sa.String(255), nullable=False),
        sa.Column('host_address', sa.String(255), nullable=False),
        sa.Column('ssh_port', sa.Integer, nullable=False, default=22),
        sa.Column('ssh_user', sa.String(64), nullable=False, default='root'),
        sa.Column('credentials', sa.Text, nullable=False),
        sa.Column('host_public_key', sa.Text, nullable=True),
        sa.Column('profile_uuid', sa.String(36), nullable=False),
        sa.Column('inbound_uuid', sa.String(36), nullable=False),
        sa.Column('sni', sa.String(255), nullable=True),
        sa.Column('node_version', sa.String(32), nullable=False),
        sa.Column('paid_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(32), nullable=False, default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_infrastructure_deployments_node_id', 'infrastructure_deployments', ['node_id'])
    op.create_table('infrastructure_jobs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('node_id', sa.Integer, sa.ForeignKey('infrastructure_nodes.id'), nullable=False),
        sa.Column('deployment_id', sa.Integer, sa.ForeignKey('infrastructure_deployments.id'), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, default='pending'),
        sa.Column('stage', sa.String(64), nullable=False, default='queued'),
        sa.Column('requested_by', sa.BigInteger, nullable=False),
        sa.Column('result', sa.Text, nullable=True),
        sa.Column('cancel_requested', sa.Boolean, nullable=False, default=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_infrastructure_jobs_node_id', 'infrastructure_jobs', ['node_id'])
    op.create_index('ix_infrastructure_jobs_status', 'infrastructure_jobs', ['status'])
    op.create_table('infrastructure_reports',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('job_id', sa.Integer, sa.ForeignKey('infrastructure_jobs.id'), nullable=False, unique=True),
        sa.Column('deployment_id', sa.Integer, sa.ForeignKey('infrastructure_deployments.id'), nullable=False),
        sa.Column('level', sa.String(16), nullable=False),
        sa.Column('manifest', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_infrastructure_reports_deployment_id', 'infrastructure_reports', ['deployment_id'])
    op.create_index('uq_infrastructure_active_job', 'infrastructure_jobs', ['node_id'],
                    unique=True, postgresql_where=sa.text("status IN ('pending', 'running')"))


def downgrade():
    op.drop_table('infrastructure_reports')
    op.drop_table('infrastructure_jobs')
    op.drop_table('infrastructure_deployments')
    op.drop_table('infrastructure_nodes')
