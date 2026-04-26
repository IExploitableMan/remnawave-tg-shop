"""add runtime app settings

Revision ID: 0010_app_settings
Revises: 0009_server_report_details
Create Date: 2026-04-25 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_app_settings"
down_revision = "0009_server_report_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
