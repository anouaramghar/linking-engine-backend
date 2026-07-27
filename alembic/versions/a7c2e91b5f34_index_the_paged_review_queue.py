"""index the paged review queue

Revision ID: a7c2e91b5f34
Revises: f18b6c4d2a90
Create Date: 2026-07-27 00:00:00.000000

"""

from alembic import op

revision = "a7c2e91b5f34"
down_revision = "f18b6c4d2a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `score` was unindexed, so the queue's "best first" ordering sorted every
    # matching row per request. Ascending is deliberate: the query orders by
    # score and id both descending, which PostgreSQL serves by reading the index
    # backwards, so a second descending copy would earn nothing.
    op.create_index("ix_suggestions_queue", "suggestions", ["status", "score", "id"])
    op.create_index(
        "ix_suggestions_site_queue", "suggestions", ["site_id", "status", "score", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_suggestions_site_queue", table_name="suggestions")
    op.drop_index("ix_suggestions_queue", table_name="suggestions")
