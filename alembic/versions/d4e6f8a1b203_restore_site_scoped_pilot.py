"""restore the site-scoped limited pilot

Revision ID: d4e6f8a1b203
Revises: c3d7a9f1e204
Create Date: 2026-07-31 11:30:00.000000

The preceding revision briefly made Hybrid global on the feature branch. Keep
that published revision in the chain so a database that already reached it can
move forward normally, then return every site and the server default to the
safe Standard mode. Operators can enroll explicit sites again after upgrading.
"""

import sqlalchemy as sa
from alembic import op

revision = "d4e6f8a1b203"
down_revision = "c3d7a9f1e204"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
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
