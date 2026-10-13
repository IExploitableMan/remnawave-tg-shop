"""Allow SSH-only records without assuming one host/inbound."""
from alembic import op
import sqlalchemy as sa
revision = '0015_infrastructure_access_only'
down_revision = '0014_infrastructure_job_queue'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('infrastructure_nodes', sa.Column('host_automation', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.execute("UPDATE infrastructure_nodes n SET host_automation = false WHERE NOT EXISTS (SELECT 1 FROM infrastructure_jobs j WHERE j.node_id = n.id AND j.kind = 'provision')")
    op.add_column('infrastructure_deployments', sa.Column('panel_address', sa.String(255), nullable=True))
    op.execute('UPDATE infrastructure_deployments SET panel_address = address')
    for field in ('profile_uuid', 'inbound_uuid'):
        op.alter_column('infrastructure_deployments', field, existing_type=sa.String(36), nullable=True)


def downgrade():
    # Unknown profile bindings require explicit reconciliation before downgrade.
    for field in ('profile_uuid', 'inbound_uuid'):
        op.alter_column('infrastructure_deployments', field, existing_type=sa.String(36), nullable=False)
    op.drop_column('infrastructure_deployments', 'panel_address')
    op.drop_column('infrastructure_nodes', 'host_automation')
