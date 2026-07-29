"""add site suggestion mode

Revision ID: 6a7d9e2c4b10
Revises: f3a8b1c2d4e5
Create Date: 2026-07-29 18:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "6a7d9e2c4b10"
down_revision = "f3a8b1c2d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column(
            "suggestion_mode",
            sa.Enum(
                "standard",
                "experimental",
                name="site_suggestion_mode",
                native_enum=False,
                length=20,
            ),
            server_default="standard",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("sites", "suggestion_mode")
