"""add forwarding_cost and amazon_fee_pct to monitored_items

Revision ID: a3ae1fa206e0
Revises: 94436cae1bdb
Create Date: 2026-02-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3ae1fa206e0'
down_revision: Union[str, Sequence[str], None] = '94436cae1bdb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('forwarding_cost', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('amazon_fee_pct', sa.Float(), nullable=False, server_default='10.0'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.drop_column('amazon_fee_pct')
        batch_op.drop_column('forwarding_cost')
