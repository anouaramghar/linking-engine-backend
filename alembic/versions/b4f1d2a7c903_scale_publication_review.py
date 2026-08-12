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
    op.drop_index("ix_suggestions_publication_pending", table_name="suggestions")
    op.drop_column("job_runs", "requested_by")
    op.alter_column(
        "job_runs",
        "kind",
        existing_type=sa.String(length=32),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
