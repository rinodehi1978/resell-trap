"""add amazon_shipping_pattern column

Revision ID: 3cb89988a51c
Revises: g7b8c9d0e1f2
Create Date: 2026-02-20 19:15:47.629601

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3cb89988a51c'
down_revision: Union[str, Sequence[str], None] = 'g7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('amazon_shipping_pattern', sa.Text(), nullable=False, server_default='2_3_days')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.drop_column('amazon_shipping_pattern')
