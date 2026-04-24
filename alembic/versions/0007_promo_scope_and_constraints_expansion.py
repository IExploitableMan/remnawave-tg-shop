"""expand promo scope and constraints

Revision ID: 0007_promo_scope_constraints
Revises: 0006_promo_traffic_vouchers
Create Date: 2026-04-14 00:00:02.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_promo_scope_constraints"
down_revision = "0006_promo_traffic_vouchers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("promo_codes", sa.Column("last_activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "promo_codes",
        sa.Column(
            "registration_date_direction",
            sa.String(),
            nullable=False,
            server_default="after",
        ),
    )
    op.add_column(
        "promo_codes",
        sa.Column(
            "subscription_presence_mode",
            sa.String(),
            nullable=False,
            server_default="any",
        ),
    )
    op.add_column(
        "promo_codes",
        sa.Column(
            "applies_to_combined_subscription",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "promo_codes",
        sa.Column(
            "combined_discount_scope",
            sa.String(),
            nullable=False,
            server_default="base_only",
        ),
    )

    op.execute(
        """
        UPDATE promo_codes
        SET subscription_presence_mode = 'active_only'
        WHERE renewal_only = TRUE
        """
    )

    op.alter_column("promo_codes", "registration_date_direction", server_default=None)
    op.alter_column("promo_codes", "subscription_presence_mode", server_default=None)
    op.alter_column("promo_codes", "applies_to_combined_subscription", server_default=None)
    op.alter_column("promo_codes", "combined_discount_scope", server_default=None)


def downgrade() -> None:
    op.drop_column("promo_codes", "combined_discount_scope")
    op.drop_column("promo_codes", "applies_to_combined_subscription")
    op.drop_column("promo_codes", "subscription_presence_mode")
    op.drop_column("promo_codes", "registration_date_direction")
    op.drop_column("promo_codes", "last_activated_at")
