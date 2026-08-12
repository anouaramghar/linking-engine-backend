"""drop obsolete per-site suggestion mode

Revision ID: e8a2c4f61d90
Revises: cc1565a6df1e
Create Date: 2026-08-10 10:30:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "e8a2c4f61d90"
down_revision = "cc1565a6df1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("sites", "suggestion_mode")


def downgrade() -> None:
    op.add_column(
        "sites",
        sa.Column(
            "suggestion_mode",
            sa.String(length=20),
            server_default="experimental",
            nullable=False,
        ),
    )
