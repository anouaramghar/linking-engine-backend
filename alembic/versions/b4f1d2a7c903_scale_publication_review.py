"""Scale publication review with durable operator-owned preparation jobs.

Revision ID: b4f1d2a7c903
Revises: a1c7e93f6b25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b4f1d2a7c903"
down_revision: str | Sequence[str] | None = "a1c7e93f6b25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "job_runs",
        "kind",
        existing_type=sa.String(length=20),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.add_column("job_runs", sa.Column("requested_by", sa.String(length=255), nullable=True))
    op.create_index(
        "ix_suggestions_publication_pending",
        "suggestions",
        ["site_id", "source_article_id", "score", "id"],
        postgresql_where=sa.text("status = 'approved' AND publication_plan_id IS NULL"),
    )


def downgrade() -> None:
    """Undo the two additions. The column width is deliberately expand-only.

    `kind` was widened to 20 -> 32 to hold 'publication_preparation', which is
    23 characters. Narrowing it back would make PostgreSQL refuse the downgrade
    the moment one such row exists, and those rows are operational evidence:
    who asked for a preparation, when, and what it produced. Deleting them or
    relabelling them 'publication' to fit the old width would corrupt the audit
    trail to satisfy a schema step.

    Leaving the column wide is safe for the application code this downgrade
    returns to. That code writes kinds of at most 20 characters, and a wider
    VARCHAR accepts every one of them.
    """
    op.drop_index("ix_suggestions_publication_pending", table_name="suggestions")
    op.drop_column("job_runs", "requested_by")
