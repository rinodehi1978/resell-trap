"""add listing_presets table

Revision ID: c8d9e0f1a2b3
Revises: a92722e911b7
Create Date: 2026-02-21 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'a92722e911b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'listing_presets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('asin', sa.Text(), nullable=False),
        sa.Column('condition', sa.Text(), nullable=False),
        sa.Column('condition_note', sa.Text(), server_default='', nullable=False),
        sa.Column('shipping_pattern', sa.Text(), server_default='2_3_days', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_listing_presets_asin', 'listing_presets', ['asin'])


def downgrade() -> None:
    op.drop_index('ix_listing_presets_asin', table_name='listing_presets')
    op.drop_table('listing_presets')
