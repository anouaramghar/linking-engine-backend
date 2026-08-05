"""index article titles for substring search

The queue's search box matches a term anywhere in a title, which is a leading
wildcard — no B-tree can serve it. A trigram GIN index can, turning what would
otherwise be a sequential scan over every article on every keystroke into an
index lookup.

Revision ID: c5f3a91b7d24
Revises: d7a4c2e9f018
Create Date: 2026-08-03
"""

from alembic import op

revision = "c5f3a91b7d24"
down_revision = "d7a4c2e9f018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same posture as the initial schema's `vector`: dev and CI create it here,
    # and a managed instance that already has it is unaffected.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # gin_trgm_ops rather than the default: the search is `ILIKE '%term%'`, and
    # only the trigram operator class answers that.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_articles_title_trgm "
        "ON articles USING gin (title gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_title_trgm")
    # The extension is deliberately left in place. Dropping it would take any
    # other trigram index in the database with it.
