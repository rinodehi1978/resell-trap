"""initial schema

Revision ID: 3720af61236b
Revises:
Create Date: 2026-02-19 12:24:51.886066

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3720af61236b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monitored_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auction_id", sa.Text, unique=True, index=True, nullable=False),
        sa.Column("title", sa.Text, server_default=""),
        sa.Column("url", sa.Text, server_default=""),
        sa.Column("image_url", sa.Text, server_default=""),
        sa.Column("category_id", sa.Text, server_default=""),
        sa.Column("seller_id", sa.Text, server_default=""),
        sa.Column("current_price", sa.Integer, server_default="0"),
        sa.Column("start_price", sa.Integer, server_default="0"),
        sa.Column("buy_now_price", sa.Integer, server_default="0"),
        sa.Column("win_price", sa.Integer, server_default="0"),
        sa.Column("start_time", sa.DateTime, nullable=True),
        sa.Column("end_time", sa.DateTime, nullable=True),
        sa.Column("bid_count", sa.Integer, server_default="0"),
        sa.Column("status", sa.Text, server_default="active"),
        sa.Column("check_interval_seconds", sa.Integer, server_default="300"),
        sa.Column("auto_adjust_interval", sa.Boolean, server_default="1"),
        sa.Column("is_monitoring_active", sa.Boolean, server_default="1"),
        sa.Column("last_checked_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("notes", sa.Text, server_default=""),
        # Amazon integration
        sa.Column("amazon_asin", sa.Text, nullable=True, index=True),
        sa.Column("amazon_sku", sa.Text, nullable=True),
        sa.Column("amazon_listing_status", sa.Text, nullable=True),
        sa.Column("amazon_price", sa.Integer, nullable=True),
        sa.Column("estimated_win_price", sa.Integer, server_default="0"),
        sa.Column("shipping_cost", sa.Integer, server_default="0"),
        sa.Column("amazon_margin_pct", sa.Float, server_default="15.0"),
        sa.Column("amazon_last_synced_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "status_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("monitored_items.id"), nullable=False),
        sa.Column("auction_id", sa.Text, nullable=False),
        sa.Column("change_type", sa.Text, nullable=False),
        sa.Column("old_status", sa.Text, nullable=True),
        sa.Column("new_status", sa.Text, nullable=True),
        sa.Column("old_price", sa.Integer, nullable=True),
        sa.Column("new_price", sa.Integer, nullable=True),
        sa.Column("old_bid_count", sa.Integer, nullable=True),
        sa.Column("new_bid_count", sa.Integer, nullable=True),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "notification_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("monitored_items.id"), nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("message", sa.Text, server_default=""),
        sa.Column("success", sa.Boolean, server_default="1"),
        sa.Column("sent_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notification_log")
    op.drop_table("status_history")
    op.drop_table("monitored_items")
