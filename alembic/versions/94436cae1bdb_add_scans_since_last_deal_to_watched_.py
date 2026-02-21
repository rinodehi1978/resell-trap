"""add scans_since_last_deal to watched_keywords

Revision ID: 94436cae1bdb
Revises: e02a5554fe00
Create Date: 2026-02-21 11:05:55.380659

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '94436cae1bdb'
down_revision: Union[str, Sequence[str], None] = 'e02a5554fe00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('watched_keywords', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scans_since_last_deal', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('watched_keywords', schema=None) as batch_op:
        batch_op.drop_column('scans_since_last_deal')
