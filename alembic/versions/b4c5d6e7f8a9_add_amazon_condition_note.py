"""add amazon_condition_note to monitored_items

Revision ID: b4c5d6e7f8a9
Revises: a3ae1fa206e0
Create Date: 2026-02-21 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3ae1fa206e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('amazon_condition_note', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('monitored_items', schema=None) as batch_op:
        batch_op.drop_column('amazon_condition_note')
