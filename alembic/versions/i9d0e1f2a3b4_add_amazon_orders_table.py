"""Add amazon_orders table for order tracking and sales analysis.

Revision ID: i9d0e1f2a3b4
Revises: h8c9d0e1f2a3
Create Date: 2026-03-01
"""

from alembic import op
import sqlalchemy as sa

revision = "i9d0e1f2a3b4"
down_revision = "h8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "amazon_orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("amazon_order_id", sa.Text, nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("monitored_items.id"), nullable=True),
        sa.Column("auction_id", sa.Text, server_default=""),
        sa.Column("sku", sa.Text, server_default=""),
        sa.Column("asin", sa.Text, server_default=""),
        sa.Column("title", sa.Text, server_default=""),
        sa.Column("order_total", sa.Integer, server_default="0"),
        sa.Column("order_status", sa.Text, server_default=""),
        sa.Column("purchase_date", sa.DateTime, nullable=True),
        sa.Column("notified_at", sa.DateTime, nullable=True),
        sa.Column("notification_success", sa.Boolean, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_amazon_orders_amazon_order_id", "amazon_orders", ["amazon_order_id"], unique=True)


def downgrade() -> None:
    op.drop_table("amazon_orders")
