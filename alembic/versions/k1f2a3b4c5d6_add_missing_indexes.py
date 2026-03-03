"""Add missing indexes for frequently queried columns.

Revision ID: k1f2a3b4c5d6
Revises: j0e1f2a3b4c5
Create Date: 2026-03-03
"""

from alembic import op

revision = "k1f2a3b4c5d6"
down_revision = "j0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.create_index("ix_monitored_items_status", ["status"])
        batch_op.create_index("ix_monitored_items_is_monitoring_active", ["is_monitoring_active"])
        batch_op.create_index("ix_monitored_items_amazon_sku", ["amazon_sku"])
        batch_op.create_index("ix_monitored_items_amazon_listing_status", ["amazon_listing_status"])

    with op.batch_alter_table("status_history") as batch_op:
        batch_op.create_index("ix_status_history_item_id", ["item_id"])

    with op.batch_alter_table("notification_log") as batch_op:
        batch_op.create_index("ix_notification_log_item_id", ["item_id"])

    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.create_index("ix_deal_alerts_keyword_id", ["keyword_id"])
        batch_op.create_index("ix_deal_alerts_amazon_asin", ["amazon_asin"])
        batch_op.create_index("ix_deal_alerts_status", ["status"])


def downgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.drop_index("ix_deal_alerts_status")
        batch_op.drop_index("ix_deal_alerts_amazon_asin")
        batch_op.drop_index("ix_deal_alerts_keyword_id")

    with op.batch_alter_table("notification_log") as batch_op:
        batch_op.drop_index("ix_notification_log_item_id")

    with op.batch_alter_table("status_history") as batch_op:
        batch_op.drop_index("ix_status_history_item_id")

    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.drop_index("ix_monitored_items_amazon_listing_status")
        batch_op.drop_index("ix_monitored_items_amazon_sku")
        batch_op.drop_index("ix_monitored_items_is_monitoring_active")
        batch_op.drop_index("ix_monitored_items_status")
