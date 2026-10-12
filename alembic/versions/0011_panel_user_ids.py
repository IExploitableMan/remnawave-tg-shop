"""Preserve user references while adding panel-scoped numeric IDs."""
from alembic import op
import sqlalchemy as sa

revision = "0011_panel_user_ids"
down_revision = "0010_app_settings"
branch_labels = None
depends_on = None


def upgrade():
    for table, fields in [("users", ["panel_user_id", "addon_panel_user_id"]),
                          ("subscriptions", ["panel_user_id"])]:
        for field in fields:
            op.add_column(table, sa.Column(field, sa.BigInteger(), nullable=True))
            op.create_index(f"ix_{table}_{field}", table, [field])
    op.create_table("panel_user_identities",
        sa.Column("panel_scope", sa.String(64), primary_key=True),
        sa.Column("panel_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("reference", sa.String(36), nullable=False),
        sa.UniqueConstraint("panel_scope", "reference"))


def downgrade():
    op.drop_table("panel_user_identities")
    for table, fields in [("subscriptions", ["panel_user_id"]),
                          ("users", ["addon_panel_user_id", "panel_user_id"])]:
        for field in fields:
            op.drop_index(f"ix_{table}_{field}", table_name=table)
            op.drop_column(table, field)
