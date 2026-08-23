"""Queue on rank_score instead of cosine score

The review queue used to sort, paginate and filter on `score`, which is cosine
similarity. On a real corpus that number sits in a very narrow band — a whole
queue of 92-93% — so it neither ordered the queue usefully nor told an editor
anything by being drawn as a meter.

`rank_score` is the strength of the signal that actually chose each row, on a
0-1 scale. For a fusion-ordered hybrid row that is its weighted-RRF score over
the ceiling RRF can reach; for every other method the fusion did not decide the
order and cosine stands in, so those rows keep the position they already had.

`score` is untouched and still means cosine everywhere it is read.

Revision ID: j7a8b9c0d1e2
Revises: i6f7a8b9c0d1
Create Date: 2026-08-23

"""

import sqlalchemy as sa
from alembic import op

revision = "j7a8b9c0d1e2"
down_revision = "i6f7a8b9c0d1"
branch_labels = None
depends_on = None


# The four indexes the queue reads, keyed on whichever column it orders by.
# `ix_suggestions_publication_pending` is deliberately absent: publication
# arbitrates anchors per source article with its own BM25-then-cosine key, so it
# is not a queue read and does not follow this move.
def _queue_indexes(column: str) -> None:
    op.create_index("ix_suggestions_queue", "suggestions", ["status", column, "id"])
    op.create_index(
        "ix_suggestions_site_queue", "suggestions", ["site_id", "status", column, "id"]
    )
    op.create_index(
        "ix_suggestions_active_queue",
        "suggestions",
        [column, "id"],
        postgresql_where=sa.text("status <> 'expired'"),
    )
    op.create_index(
        "ix_suggestions_site_active_queue",
        "suggestions",
        ["site_id", column, "id"],
        postgresql_where=sa.text("status <> 'expired'"),
    )


def _drop_queue_indexes() -> None:
    for name in (
        "ix_suggestions_queue",
        "ix_suggestions_site_queue",
        "ix_suggestions_active_queue",
        "ix_suggestions_site_active_queue",
    ):
        op.drop_index(name, table_name="suggestions")


def _snapshot_immutability_trigger(*columns: str) -> None:
    """Point the ranking-evidence guard at an explicit column list.

    The guarded columns live in the trigger definition, not the function, so
    adding one means recreating the trigger. Callers must have finished writing
    those columns before this runs.
    """

    op.execute("DROP TRIGGER IF EXISTS trg_suggestion_ranking_snapshot_immutable ON suggestions")
    guard = "\n               OR ".join(
        f"OLD.{column} IS DISTINCT FROM NEW.{column}" for column in columns
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION prevent_suggestion_ranking_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF {guard} THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = 'suggestion ranking snapshot is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_suggestion_ranking_snapshot_immutable
        BEFORE UPDATE OF {", ".join(columns)} ON suggestions
        FOR EACH ROW EXECUTE FUNCTION prevent_suggestion_ranking_snapshot_mutation()
        """
    )


def upgrade() -> None:
    op.add_column("suggestions", sa.Column("rank_score", sa.Float(), nullable=True))

    # Re-derive the stored fusion score against the ceiling that was in force
    # when the row was written. Newer rows record that ceiling directly; older
    # ones carry the weights it was computed from. Reading the current setting
    # instead would rescale historical rows against a ceiling their ranking
    # never saw.
    op.execute(
        """
        WITH fused AS (
            SELECT
                id,
                (score_components->>'fusion_score')::double precision AS fusion_score,
                NULLIF(
                    COALESCE(
                        (score_components->'fusion'->>'ceiling')::double precision,
                        (
                            (score_components->'fusion'->>'dense_weight')::double precision
                            + (score_components->'fusion'->>'lexical_weight')::double precision
                        ) / NULLIF(
                            (score_components->'fusion'->>'rank_constant')::double precision + 1,
                            0
                        )
                    ),
                    0
                ) AS ceiling
            FROM suggestions
            WHERE method = 'hybrid_bm25'
              AND score_components->>'final_order' = 'wrrf'
              AND jsonb_typeof(score_components->'fusion_score') = 'number'
        )
        UPDATE suggestions AS s
        SET rank_score = LEAST(1.0, GREATEST(0.0, fused.fusion_score / fused.ceiling))
        FROM fused
        WHERE fused.id = s.id
          AND fused.ceiling IS NOT NULL
        """
    )
    # Every other row: BM25-ordered, baseline cosine, external search, or written
    # before the fusion recorded its score. Cosine is what ordered them before
    # this migration, so falling back to it leaves their order unchanged.
    op.execute("UPDATE suggestions SET rank_score = score WHERE rank_score IS NULL")
    op.alter_column("suggestions", "rank_score", nullable=False)

    _drop_queue_indexes()
    _queue_indexes("rank_score")

    # Only now that the backfill has finished can the column be frozen.
    _snapshot_immutability_trigger(
        "score_components",
        "retrieval_version",
        "ranking_version",
        "final_rank",
        "rank_score",
        "feature_snapshot",
    )


def downgrade() -> None:
    _snapshot_immutability_trigger(
        "score_components",
        "retrieval_version",
        "ranking_version",
        "final_rank",
        "feature_snapshot",
    )
    _drop_queue_indexes()
    _queue_indexes("score")
    op.drop_column("suggestions", "rank_score")
