"""add watched_keywords and deal_alerts

Revision ID: e33eefc791a3
Revises: 017c4728d6f6
Create Date: 2026-02-19 14:19:24.179770

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e33eefc791a3'
down_revision: Union[str, Sequence[str], None] = '017c4728d6f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watched_keywords",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("keyword", sa.Text, unique=True, index=True, nullable=False),
        sa.Column("is_active", sa.Boolean, server_default="1", nullable=False),
        sa.Column("last_scanned_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("notes", sa.Text, server_default="", nullable=False),
    )

    op.create_table(
        "deal_alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("keyword_id", sa.Integer, sa.ForeignKey("watched_keywords.id"), nullable=False),
        sa.Column("yahoo_auction_id", sa.Text, index=True, nullable=False),
        sa.Column("amazon_asin", sa.Text, nullable=False),
        sa.Column("yahoo_title", sa.Text, server_default="", nullable=False),
        sa.Column("yahoo_url", sa.Text, server_default="", nullable=False),
        sa.Column("yahoo_price", sa.Integer, server_default="0", nullable=False),
        sa.Column("sell_price", sa.Integer, server_default="0", nullable=False),
        sa.Column("gross_profit", sa.Integer, server_default="0", nullable=False),
        sa.Column("gross_margin_pct", sa.Float, server_default="0.0", nullable=False),
        sa.Column("notified_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("yahoo_auction_id", "amazon_asin", name="uq_deal_alert"),
    )


def downgrade() -> None:
    op.drop_table("deal_alerts")
    op.drop_table("watched_keywords")
