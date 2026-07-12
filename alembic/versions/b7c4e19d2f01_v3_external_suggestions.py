"""v3: external suggestions — nullable target + external url/title/trust_score

Revision ID: b7c4e19d2f01
Revises: 9511e0ed9499
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c4e19d2f01"
down_revision = "9511e0ed9499"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("suggestions", "target_article_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("suggestions", sa.Column("external_url", sa.String(2048), nullable=True))
    op.add_column("suggestions", sa.Column("external_title", sa.Text(), nullable=True))
    op.add_column("suggestions", sa.Column("trust_score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("suggestions", "trust_score")
    op.drop_column("suggestions", "external_title")
    op.drop_column("suggestions", "external_url")
    op.execute("DELETE FROM suggestions WHERE target_article_id IS NULL")
    op.alter_column("suggestions", "target_article_id", existing_type=sa.Integer(), nullable=False)
