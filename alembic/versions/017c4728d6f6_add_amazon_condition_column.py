"""add amazon_condition column

Revision ID: 017c4728d6f6
Revises: 3720af61236b
Create Date: 2026-02-19 14:03:55.369361

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '017c4728d6f6'
down_revision: Union[str, Sequence[str], None] = '3720af61236b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.add_column(
            sa.Column("amazon_condition", sa.Text(), server_default="used_very_good", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("monitored_items") as batch_op:
        batch_op.drop_column("amazon_condition")
