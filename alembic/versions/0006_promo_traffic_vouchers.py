"""add promo traffic vouchers

Revision ID: 0006_promo_traffic_vouchers
Revises: 0005_promo_constraints
Create Date: 2026-04-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0006_promo_traffic_vouchers"
down_revision = "0005_promo_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("promo_codes", sa.Column("traffic_amount_gb", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("promo_codes", "traffic_amount_gb")
