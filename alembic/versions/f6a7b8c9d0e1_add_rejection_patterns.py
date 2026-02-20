"""add rejection_patterns table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rejection_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pattern_type", sa.Text(), nullable=False),
        sa.Column("pattern_key", sa.Text(), server_default="", nullable=False),
        sa.Column("pattern_data", sa.Text(), server_default="{}", nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.5", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("pattern_type", "pattern_key", name="uq_rejection_pattern"),
    )
    op.create_index("ix_rejection_patterns_pattern_type", "rejection_patterns", ["pattern_type"])


def downgrade() -> None:
    op.drop_index("ix_rejection_patterns_pattern_type")
    op.drop_table("rejection_patterns")
