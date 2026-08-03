"""restore Hybrid as the global suggestion standard

Revision ID: e7b4c9d2a601
Revises: d4e6f8a1b203
Create Date: 2026-07-31 12:00:00.000000

The site-scoped rollout remains in the migration history, but the product
contract is global Hybrid again. Existing suggestions are intentionally left
untouched; this only restores the compatibility field and its default.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7b4c9d2a601"
down_revision = "d4e6f8a1b203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "sites",
        "suggestion_mode",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default="experimental",
    )
    op.execute(
        sa.text(
            "UPDATE sites SET suggestion_mode = 'experimental' "
            "WHERE suggestion_mode <> 'experimental'"
        )
    )


def downgrade() -> None:
    op.alter_column(
        "sites",
        "suggestion_mode",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default="standard",
    )
    op.execute(
        sa.text("UPDATE sites SET suggestion_mode = 'standard' WHERE suggestion_mode <> 'standard'")
    )
