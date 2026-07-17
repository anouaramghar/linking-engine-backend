"""add embedding freshness metadata

Revision ID: b72c91e4a6f8
Revises: 4d3f7b2c9a10
Create Date: 2026-07-16 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b72c91e4a6f8"
down_revision = "4d3f7b2c9a10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embeddings",
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "embeddings",
        sa.Column("input_recipe_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "embeddings",
        sa.Column("vector_size", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("embeddings", "vector_size")
    op.drop_column("embeddings", "input_recipe_version")
    op.drop_column("embeddings", "content_fingerprint")
