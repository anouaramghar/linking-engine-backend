"""index the default active review queue

Revision ID: c4f8a2d91e73
Revises: a7c2e91b5f34
Create Date: 2026-07-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c4f8a2d91e73"
down_revision = "a7c2e91b5f34"
branch_labels = None
depends_on = None


def _drop_invalid_index(name: str) -> None:
    """Remove a failed concurrent build before retrying this unstamped revision."""

    valid = (
        op.get_bind()
        .execute(
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
        )
        .scalar_one_or_none()
    )
    if valid is False:
        # The autocommit block is required for both DROP/CREATE CONCURRENTLY.
        op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"'))


def upgrade() -> None:
    # A status-leading index cannot preserve global score order when the default
    # queue spans every non-expired status. These partial indexes contain exactly
    # that active set and remain writable during an online deployment.
    with op.get_context().autocommit_block():
        _drop_invalid_index("ix_suggestions_active_queue")
        op.create_index(
            "ix_suggestions_active_queue",
            "suggestions",
            ["score", "id"],
            postgresql_where=sa.text("status <> 'expired'"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        _drop_invalid_index("ix_suggestions_site_active_queue")
        op.create_index(
            "ix_suggestions_site_active_queue",
            "suggestions",
            ["site_id", "score", "id"],
            postgresql_where=sa.text("status <> 'expired'"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_suggestions_site_active_queue",
            table_name="suggestions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_suggestions_active_queue",
            table_name="suggestions",
            postgresql_concurrently=True,
            if_exists=True,
        )
