"""Add amazon_image_urls to monitored_items

Revision ID: j0e1f2a3b4c5
Revises: i9d0e1f2a3b4
Create Date: 2026-03-01
"""
from alembic import op
import sqlalchemy as sa

revision = "j0e1f2a3b4c5"
down_revision = "i9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.add_column(
            sa.Column("amazon_image_urls", sa.Text(), server_default="", nullable=False),
        )


def downgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.drop_column("amazon_image_urls")
