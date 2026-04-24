"""add server reports

Revision ID: 0008_server_reports
Revises: 0007_promo_scope_constraints
Create Date: 2026-04-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_server_reports"
down_revision = "0007_promo_scope_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_reports",
        sa.Column("report_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index(op.f("ix_server_reports_user_id"), "server_reports", ["user_id"], unique=False)
    op.create_index(op.f("ix_server_reports_issue_type"), "server_reports", ["issue_type"], unique=False)
    op.create_index(op.f("ix_server_reports_status"), "server_reports", ["status"], unique=False)
    op.create_index(op.f("ix_server_reports_created_at"), "server_reports", ["created_at"], unique=False)

    op.create_table(
        "server_report_hosts",
        sa.Column("report_host_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("host_uuid", sa.String(), nullable=False),
        sa.Column("host_name", sa.String(), nullable=False),
        sa.Column("host_address", sa.String(), nullable=True),
        sa.Column("node_uuid", sa.String(), nullable=True),
        sa.Column("node_name", sa.String(), nullable=True),
        sa.Column("profile_kind", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["server_reports.report_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("report_host_id"),
    )
    op.create_index(op.f("ix_server_report_hosts_report_id"), "server_report_hosts", ["report_id"], unique=False)
    op.create_index(op.f("ix_server_report_hosts_host_uuid"), "server_report_hosts", ["host_uuid"], unique=False)
    op.create_index(op.f("ix_server_report_hosts_node_uuid"), "server_report_hosts", ["node_uuid"], unique=False)

    op.create_table(
        "admin_server_report_preferences",
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("reports_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("admin_id"),
    )
    op.create_index(
        op.f("ix_admin_server_report_preferences_admin_id"),
        "admin_server_report_preferences",
        ["admin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_server_report_preferences_reports_enabled"),
        "admin_server_report_preferences",
        ["reports_enabled"],
        unique=False,
    )
    op.alter_column("server_reports", "status", server_default=None)
    op.alter_column("admin_server_report_preferences", "reports_enabled", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_server_report_preferences_reports_enabled"), table_name="admin_server_report_preferences")
    op.drop_index(op.f("ix_admin_server_report_preferences_admin_id"), table_name="admin_server_report_preferences")
    op.drop_table("admin_server_report_preferences")
    op.drop_index(op.f("ix_server_report_hosts_node_uuid"), table_name="server_report_hosts")
    op.drop_index(op.f("ix_server_report_hosts_host_uuid"), table_name="server_report_hosts")
    op.drop_index(op.f("ix_server_report_hosts_report_id"), table_name="server_report_hosts")
    op.drop_table("server_report_hosts")
    op.drop_index(op.f("ix_server_reports_created_at"), table_name="server_reports")
    op.drop_index(op.f("ix_server_reports_status"), table_name="server_reports")
    op.drop_index(op.f("ix_server_reports_issue_type"), table_name="server_reports")
    op.drop_index(op.f("ix_server_reports_user_id"), table_name="server_reports")
    op.drop_table("server_reports")
