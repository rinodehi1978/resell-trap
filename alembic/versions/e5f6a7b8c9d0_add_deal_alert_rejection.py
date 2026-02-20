"""add rejection fields to deal_alerts

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.add_column(sa.Column("status", sa.Text(), server_default="active", nullable=False))
        batch_op.add_column(sa.Column("rejection_reason", sa.Text(), server_default="", nullable=False))
        batch_op.add_column(sa.Column("rejection_note", sa.Text(), server_default="", nullable=False))
        batch_op.add_column(sa.Column("rejected_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.drop_column("rejected_at")
        batch_op.drop_column("rejection_note")
        batch_op.drop_column("rejection_reason")
        batch_op.drop_column("status")
