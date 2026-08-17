"""Turn editorial feedback reranking off by default.

The feature shipped enabled for every site. That made an unproven signal the
production default: ten mixed approved/rejected rows are enough to activate it,
bulk decisions count the same as considered ones, and the resulting order gets
20% of the ranking weight without ever having been measured against a held-out
set. The evidence plan (docs/superpowers/plans/2026-08-11-evidence-driven-
operations.md) requires three representative sites, 100 individual labels per
site and a versioned result before a ranking default may move.

Existing rows are turned off with the default. The column has been on since it
was added and no site has ever chosen it, so there is no operator decision here
to preserve — every ``true`` in the table is this default, not a choice. An
operator who wants it back switches it on per site, and owns that.

Revision ID: d1b7f4c2e809
Revises: c9a4e17b3d52
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d1b7f4c2e809"
down_revision: str | Sequence[str] | None = "c9a4e17b3d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sites",
        "editorial_feedback_enabled",
        server_default=sa.text("false"),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
    op.execute(sa.text("UPDATE sites SET editorial_feedback_enabled = false"))


def downgrade() -> None:
    # Only the default returns. Re-enabling every site would undo an operator's
    # per-site decision, which is exactly the mistake this migration corrects.
    op.alter_column(
        "sites",
        "editorial_feedback_enabled",
        server_default=sa.text("true"),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
