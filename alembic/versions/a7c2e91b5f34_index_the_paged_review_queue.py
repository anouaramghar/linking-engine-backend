"""index the paged review queue

Revision ID: a7c2e91b5f34
Revises: f18b6c4d2a90
Create Date: 2026-07-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a7c2e91b5f34"
down_revision = "f18b6c4d2a90"
branch_labels = None
depends_on = None


def _drop_invalid_index(name: str) -> None:
    """Remove a failed concurrent build before retrying this unstamped revision."""

    valid = op.get_bind().execute(
        sa.text(
            """
            SELECT idx.indisvalid
            FROM pg_index AS idx
            JOIN pg_class AS relation ON relation.oid = idx.indexrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE relation.relname = :name
              AND namespace.nspname = current_schema()
            """
        ),
        {"name": name},
    ).scalar_one_or_none()
    if valid is False:
        # The autocommit block is required for both DROP/CREATE CONCURRENTLY.
        op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"'))


def upgrade() -> None:
    # `score` was unindexed, so the queue's "best first" ordering sorted every
    # matching row per request. Ascending is deliberate: the query orders by
    # score and id both descending, which PostgreSQL serves by reading the index
    # backwards, so a second descending copy would earn nothing.
    # CONCURRENTLY keeps ingestion, publication, and review writes moving while
    # PostgreSQL scans the existing production table. It cannot run inside
    # Alembic's normal migration transaction.
    with op.get_context().autocommit_block():
        _drop_invalid_index("ix_suggestions_queue")
        op.create_index(
            "ix_suggestions_queue",
            "suggestions",
            ["status", "score", "id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        _drop_invalid_index("ix_suggestions_site_queue")
        op.create_index(
            "ix_suggestions_site_queue",
            "suggestions",
            ["site_id", "status", "score", "id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_suggestions_site_queue",
            table_name="suggestions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_suggestions_queue",
            table_name="suggestions",
            postgresql_concurrently=True,
            if_exists=True,
        )
