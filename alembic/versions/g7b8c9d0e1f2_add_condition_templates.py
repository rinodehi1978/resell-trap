"""Add condition_templates table.

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "g7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "condition_templates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("condition_type", sa.Text, unique=True, nullable=False),
        sa.Column("title", sa.Text, server_default=""),
        sa.Column("body", sa.Text, server_default=""),
        sa.Column("updated_at", sa.DateTime),
    )

    # Insert default templates
    op.execute(
        "INSERT INTO condition_templates (condition_type, title, body) VALUES "
        "('used_like_new', 'ほぼ新品', "
        "'ほぼ新品の状態です。\n動作確認済み。\n目立つ傷や汚れはありません。\n付属品は全て揃っています。'),"
        "('used_very_good', '非常に良い', "
        "'中古品ですが、状態は非常に良好です。\n動作確認済み。\n目立つ傷や汚れはありません。\n付属品についてはお問い合わせください。'),"
        "('used_good', '良い', "
        "'中古品です。使用感はありますが、動作に問題はありません。\n動作確認済み。\n多少の傷や汚れがある場合があります。'),"
        "('used_acceptable', '可', "
        "'中古品です。使用感があります。\n動作確認済み。\n傷や汚れがある場合がありますが、使用には問題ありません。')"
    )


def downgrade() -> None:
    op.drop_table("condition_templates")
