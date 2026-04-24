"""add promo constraints

Revision ID: 0005_promo_constraints
Revises: 0004_addon_plan_support
Create Date: 2026-04-14 00:00:01.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_promo_constraints"
down_revision: Union[str, Sequence[str], None] = "0004_addon_plan_support"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promo_codes", sa.Column("max_discount_amount", sa.Float(), nullable=True))
    op.add_column("promo_codes", sa.Column("min_user_registration_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("promo_codes", sa.Column("renewal_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("promo_codes", "renewal_only", server_default=None)


def downgrade() -> None:
    op.drop_column("promo_codes", "renewal_only")
    op.drop_column("promo_codes", "min_user_registration_date")
    op.drop_column("promo_codes", "max_discount_amount")
