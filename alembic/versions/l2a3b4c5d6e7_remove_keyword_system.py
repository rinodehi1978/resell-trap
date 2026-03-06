"""Remove keyword management system - add search_keyword to deal_alerts, make keyword_id nullable.

Revision ID: l2a3b4c5d6e7
Revises: k1f2a3b4c5d6
Create Date: 2026-03-06
"""

from alembic import op
import sqlalchemy as sa

revision = "l2a3b4c5d6e7"
down_revision = "k1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.add_column(
            sa.Column("search_keyword", sa.Text(), server_default="", nullable=True)
        )
        batch_op.alter_column(
            "keyword_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    # Backfill search_keyword from watched_keywords
    op.execute(
        """
        UPDATE deal_alerts SET search_keyword = (
            SELECT keyword FROM watched_keywords
            WHERE watched_keywords.id = deal_alerts.keyword_id
        ) WHERE keyword_id IS NOT NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.drop_column("search_keyword")
        batch_op.alter_column(
            "keyword_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
