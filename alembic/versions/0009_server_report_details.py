"""add server report details

Revision ID: 0009_server_report_details
Revises: 0008_server_reports
Create Date: 2026-04-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_server_report_details"
down_revision = "0008_server_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_reports", sa.Column("details", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_reports", "details")
