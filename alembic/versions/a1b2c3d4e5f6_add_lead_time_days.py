"""add lead_time_days to monitored_items

Revision ID: a1b2c3d4e5f6
Revises: e33eefc791a3
Create Date: 2026-02-19 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e33eefc791a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.add_column(
            sa.Column("amazon_lead_time_days", sa.Integer, server_default="4", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.drop_column("amazon_lead_time_days")
