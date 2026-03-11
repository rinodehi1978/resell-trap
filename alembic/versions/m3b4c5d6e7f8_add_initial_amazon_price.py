"""add initial_amazon_price column

Revision ID: m3b4c5d6e7f8
Revises: l2a3b4c5d6e7
Create Date: 2026-03-11
"""
from alembic import op
import sqlalchemy as sa

revision = "m3b4c5d6e7f8"
down_revision = "l2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.add_column(
            sa.Column("initial_amazon_price", sa.Integer(), nullable=True)
        )
    # Backfill: set initial_amazon_price = amazon_price for existing items
    op.execute(
        "UPDATE monitored_items SET initial_amazon_price = amazon_price "
        "WHERE amazon_price IS NOT NULL AND initial_amazon_price IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.drop_column("initial_amazon_price")
