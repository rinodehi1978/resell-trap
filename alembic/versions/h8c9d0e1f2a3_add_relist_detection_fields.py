"""Add ended_at and relist_count to monitored_items for auto-relist detection.

Revision ID: h8c9d0e1f2a3
Revises: g7b8c9d0e1f2
Create Date: 2026-02-25
"""

from alembic import op
import sqlalchemy as sa

revision = "h8c9d0e1f2a3"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.add_column(
            sa.Column("ended_at", sa.DateTime, nullable=True)
        )
        batch_op.add_column(
            sa.Column("relist_count", sa.Integer, server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.drop_column("relist_count")
        batch_op.drop_column("ended_at")
