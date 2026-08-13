"""make Hybrid/BM25 the global suggestion method

Revision ID: c3d7a9f1e204
Revises: b8e5f1a3c027
Create Date: 2026-07-30 14:20:00.000000

The application no longer offers a per-site ranking choice. The legacy
``suggestion_mode`` column stays in place for one release so older API clients
can read a compatible response during a rolling deployment, but every existing
and new site is pinned to ``experimental`` (the value that represents Hybrid in
the pre-global schema).

Downgrading this revision is the deliberate operational rollback: it restores
the former Standard/cosine default for every site. It does not delete or rewrite
suggestions; the preceding score-components migration continues to protect
Hybrid rows if an operator attempts to downgrade farther.
"""

import sqlalchemy as sa
from alembic import op

revision = "c3d7a9f1e204"
down_revision = "b8e5f1a3c027"
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
