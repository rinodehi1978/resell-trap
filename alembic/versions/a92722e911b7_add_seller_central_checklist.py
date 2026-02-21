"""add seller_central_checklist

Revision ID: a92722e911b7
Revises: b4c5d6e7f8a9
Create Date: 2026-02-21 15:28:37.622508

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a92722e911b7'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seller_central_checklist', sa.Text(), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.drop_column('seller_central_checklist')
