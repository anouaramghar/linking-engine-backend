"""add suggestion score components

Adds the column that carries a pilot row's truthful explanation: the BM25 score
that selected and ordered it, its fusion and per-retriever ranks, and the recipe
names. `suggestions.score` stays cosine similarity for every method, so the
dashboard percentage and its thresholds keep one meaning.

Downgrading is refused while `hybrid_bm25` rows exist. Dropping this column
would leave those rows in the queue with no record of how they were chosen, in
front of an application whose method enum does not include `hybrid_bm25` — an
unreadable row is worse than a blocked rollback, and unlike the rollback it
cannot be undone. Clearing the rows first is a deliberate, site-scoped editorial
action, so it belongs to an operator rather than to a schema migration:

    python -m scripts.expire_pending_suggestions --site-id N --method hybrid_bm25 --yes

Revision ID: b8e5f1a3c027
Revises: 6a7d9e2c4b10
Create Date: 2026-07-30 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8e5f1a3c027"
down_revision = "6a7d9e2c4b10"
branch_labels = None
depends_on = None

PILOT_METHOD = "hybrid_bm25"


def _refuse_while_pilot_rows_exist() -> None:
    """Abort the downgrade before it changes anything.

    Runs first, and reports the rows by status, because "expire the pending ones"
    and "you are about to orphan applied editorial history" are different
    situations for whoever is holding the rollback.
    """
    connection = op.get_bind()
    if not connection.dialect.has_table(connection, "suggestions"):
        return
    rows = connection.execute(
        sa.text(
            "SELECT status, count(*) AS total FROM suggestions "
            "WHERE method = :method GROUP BY status ORDER BY status"
        ),
        {"method": PILOT_METHOD},
    ).all()
    if not rows:
        return
    breakdown = ", ".join(f"{row.status}={row.total}" for row in rows)
    total = sum(row.total for row in rows)
    raise RuntimeError(
        f"Refusing to downgrade: {total} {PILOT_METHOD} suggestion(s) exist ({breakdown}).\n"
        "Dropping suggestions.score_components would leave them with no record of how "
        "they were selected, and the pre-pilot application does not recognize the "
        f"{PILOT_METHOD!r} method at all.\n"
        "Resolve the rows explicitly first, one site at a time:\n"
        "    python -m scripts.expire_pending_suggestions "
        "--site-id N --method hybrid_bm25 --yes\n"
        "Reviewed rows (approved/applying/applied/rejected) are editorial history and "
        "are never expired by that script; decide about them deliberately before "
        "rolling back past this revision."
    )


def upgrade() -> None:
    op.add_column(
        "suggestions",
        sa.Column("score_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    _refuse_while_pilot_rows_exist()
    op.drop_column("suggestions", "score_components")
