"""add yahoo_image_url and amazon_title to deal_alerts

Revision ID: c3d4e5f6a7b8
Revises: b7c8d9e0f1a2
Create Date: 2026-02-19
"""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.add_column(sa.Column("yahoo_image_url", sa.Text(), server_default="", nullable=False))
        batch_op.add_column(sa.Column("amazon_title", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("deal_alerts") as batch_op:
        batch_op.drop_column("amazon_title")
        batch_op.drop_column("yahoo_image_url")
