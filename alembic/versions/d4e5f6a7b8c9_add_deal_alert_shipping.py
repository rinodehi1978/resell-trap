"""add yahoo_shipping to deal_alerts

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.add_column(sa.Column("yahoo_shipping", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.drop_column("yahoo_shipping")
