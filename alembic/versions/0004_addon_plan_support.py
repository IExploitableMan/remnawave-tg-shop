"""add add-on plan support

Revision ID: 0004_addon_plan_support
Revises: 0003_promo_curr_act_not_null
Create Date: 2026-04-14 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_addon_plan_support"
down_revision: Union[str, Sequence[str], None] = "0003_promo_curr_act_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("addon_panel_user_uuid", sa.String(), nullable=True))
    op.create_index("ix_users_addon_panel_user_uuid", "users", ["addon_panel_user_uuid"], unique=True)

    op.add_column("subscriptions", sa.Column("kind", sa.String(), nullable=False, server_default="base"))
    op.add_column("subscriptions", sa.Column("included_traffic_bytes", sa.BigInteger(), nullable=True))
    op.add_column("subscriptions", sa.Column("included_traffic_remaining_bytes", sa.BigInteger(), nullable=True))
    op.add_column("subscriptions", sa.Column("traffic_cycle_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("traffic_cycle_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("traffic_warning_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("traffic_exhausted_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_subscriptions_kind", "subscriptions", ["kind"], unique=False)

    op.add_column("payments", sa.Column("kind", sa.String(), nullable=False, server_default="base_subscription"))
    op.create_index("ix_payments_kind", "payments", ["kind"], unique=False)
    op.add_column("active_discounts", sa.Column("payment_kind", sa.String(), nullable=False, server_default="base_subscription"))

    op.add_column("promo_codes", sa.Column("applies_to_base_subscription", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("promo_codes", sa.Column("applies_to_addon_subscription", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("promo_codes", sa.Column("applies_to_addon_traffic_topup", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "addon_traffic_topups",
        sa.Column("topup_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("remaining_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.payment_id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.subscription_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("topup_id"),
        sa.UniqueConstraint("payment_id"),
    )
    op.create_index("ix_addon_traffic_topups_user_id", "addon_traffic_topups", ["user_id"], unique=False)
    op.create_index("ix_addon_traffic_topups_subscription_id", "addon_traffic_topups", ["subscription_id"], unique=False)
    op.create_index("ix_addon_traffic_topups_payment_id", "addon_traffic_topups", ["payment_id"], unique=True)
    op.create_index("ix_addon_traffic_topups_expires_at", "addon_traffic_topups", ["expires_at"], unique=False)
    op.create_index("ix_addon_traffic_topups_status", "addon_traffic_topups", ["status"], unique=False)

    op.alter_column("subscriptions", "kind", server_default=None)
    op.alter_column("payments", "kind", server_default=None)
    op.alter_column("active_discounts", "payment_kind", server_default=None)
    op.alter_column("promo_codes", "applies_to_base_subscription", server_default=None)
    op.alter_column("promo_codes", "applies_to_addon_subscription", server_default=None)
    op.alter_column("promo_codes", "applies_to_addon_traffic_topup", server_default=None)
    op.alter_column("addon_traffic_topups", "status", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_addon_traffic_topups_status", table_name="addon_traffic_topups")
    op.drop_index("ix_addon_traffic_topups_expires_at", table_name="addon_traffic_topups")
    op.drop_index("ix_addon_traffic_topups_payment_id", table_name="addon_traffic_topups")
    op.drop_index("ix_addon_traffic_topups_subscription_id", table_name="addon_traffic_topups")
    op.drop_index("ix_addon_traffic_topups_user_id", table_name="addon_traffic_topups")
    op.drop_table("addon_traffic_topups")

    op.drop_column("active_discounts", "payment_kind")
    op.drop_column("promo_codes", "applies_to_addon_traffic_topup")
    op.drop_column("promo_codes", "applies_to_addon_subscription")
    op.drop_column("promo_codes", "applies_to_base_subscription")

    op.drop_index("ix_payments_kind", table_name="payments")
    op.drop_column("payments", "kind")

    op.drop_index("ix_subscriptions_kind", table_name="subscriptions")
    op.drop_column("subscriptions", "traffic_exhausted_sent_at")
    op.drop_column("subscriptions", "traffic_warning_sent_at")
    op.drop_column("subscriptions", "traffic_cycle_ends_at")
    op.drop_column("subscriptions", "traffic_cycle_started_at")
    op.drop_column("subscriptions", "included_traffic_remaining_bytes")
    op.drop_column("subscriptions", "included_traffic_bytes")
    op.drop_column("subscriptions", "kind")

    op.drop_index("ix_users_addon_panel_user_uuid", table_name="users")
    op.drop_column("users", "addon_panel_user_uuid")
