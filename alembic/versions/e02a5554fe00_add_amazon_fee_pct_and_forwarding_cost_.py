"""add amazon_fee_pct and forwarding_cost to deal_alerts

Revision ID: e02a5554fe00
Revises: 3cb89988a51c
Create Date: 2026-02-21 09:58:35.371147

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e02a5554fe00'
down_revision: Union[str, Sequence[str], None] = '3cb89988a51c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('deal_alerts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('amazon_fee_pct', sa.Float(), nullable=False, server_default='10.0'))
        batch_op.add_column(sa.Column('forwarding_cost', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('deal_alerts', schema=None) as batch_op:
        batch_op.drop_column('forwarding_cost')
        batch_op.drop_column('amazon_fee_pct')
