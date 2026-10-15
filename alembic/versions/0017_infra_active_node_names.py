"""Allow reuse of archived node names without changing archived records."""
from alembic import op
import sqlalchemy as sa

revision = '0017_infra_active_node_names'
down_revision = '0016_infrastructure_job_messages'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints('infrastructure_nodes'):
        if set(constraint['column_names']) == {'panel_scope', 'name'}:
            op.drop_constraint(constraint['name'], 'infrastructure_nodes', type_='unique')
    op.create_index('uq_infra_active_node_name', 'infrastructure_nodes', ['panel_scope', 'name'],
                    unique=True, postgresql_where=sa.text('deleted = false'))


def downgrade():
    op.drop_index('uq_infra_active_node_name', table_name='infrastructure_nodes')
    op.create_unique_constraint('infrastructure_nodes_panel_scope_name_key',
                               'infrastructure_nodes', ['panel_scope', 'name'])
