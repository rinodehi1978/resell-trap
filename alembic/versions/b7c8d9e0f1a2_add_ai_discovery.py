"""add AI discovery tables and WatchedKeyword columns

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-02-19 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add AI discovery columns to watched_keywords
    with op.batch_alter_table("watched_keywords") as batch_op:
        batch_op.add_column(sa.Column("source", sa.Text, server_default="manual", nullable=False))
        batch_op.add_column(sa.Column("parent_keyword_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("performance_score", sa.Float, server_default="0.0", nullable=False))
        batch_op.add_column(sa.Column("total_scans", sa.Integer, server_default="0", nullable=False))
        batch_op.add_column(sa.Column("total_deals_found", sa.Integer, server_default="0", nullable=False))
        batch_op.add_column(sa.Column("total_gross_profit", sa.Integer, server_default="0", nullable=False))
        batch_op.add_column(sa.Column("confidence", sa.Float, server_default="1.0", nullable=False))
        batch_op.add_column(sa.Column("auto_deactivated_at", sa.DateTime, nullable=True))
        batch_op.create_foreign_key(
            "fk_watched_keywords_parent",
            "watched_keywords",
            ["parent_keyword_id"],
            ["id"],
        )

    # Create keyword_candidates table
    op.create_table(
        "keyword_candidates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("keyword", sa.Text, nullable=False, index=True),
        sa.Column("strategy", sa.Text, server_default="", nullable=False),
        sa.Column("confidence", sa.Float, server_default="0.0", nullable=False),
        sa.Column("parent_keyword_id", sa.Integer, sa.ForeignKey("watched_keywords.id"), nullable=True),
        sa.Column("reasoning", sa.Text, server_default="", nullable=False),
        sa.Column("status", sa.Text, server_default="pending", nullable=False),
        sa.Column("validation_result", sa.Text, server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
    )

    # Create discovery_log table
    op.create_table(
        "discovery_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.Text, server_default="running", nullable=False),
        sa.Column("candidates_generated", sa.Integer, server_default="0", nullable=False),
        sa.Column("candidates_validated", sa.Integer, server_default="0", nullable=False),
        sa.Column("keywords_added", sa.Integer, server_default="0", nullable=False),
        sa.Column("keywords_deactivated", sa.Integer, server_default="0", nullable=False),
        sa.Column("keepa_tokens_used", sa.Integer, server_default="0", nullable=False),
        sa.Column("strategy_breakdown", sa.Text, server_default="{}", nullable=False),
        sa.Column("error_message", sa.Text, server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_table("discovery_log")
    op.drop_table("keyword_candidates")

    with op.batch_alter_table("watched_keywords") as batch_op:
        batch_op.drop_constraint("fk_watched_keywords_parent", type_="foreignkey")
        batch_op.drop_column("auto_deactivated_at")
        batch_op.drop_column("confidence")
        batch_op.drop_column("total_gross_profit")
        batch_op.drop_column("total_deals_found")
        batch_op.drop_column("total_scans")
        batch_op.drop_column("performance_score")
        batch_op.drop_column("parent_keyword_id")
        batch_op.drop_column("source")
