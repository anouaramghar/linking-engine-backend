"""Baseline v1: cosine top-k over pgvector, exact search (A13 — no index below ~100k vectors)."""

from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

TOP_K_SQL = text("""
SELECT a2.id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
FROM embeddings e1
JOIN articles a1 ON a1.id = e1.article_id
JOIN embeddings e2 ON e2.model = e1.model AND e2.article_id != e1.article_id
JOIN articles a2 ON a2.id = e2.article_id AND a2.site_id = a1.site_id
WHERE a1.id = :article_id
  AND e1.model = :model
  AND a2.is_active IS TRUE
  AND NOT EXISTS (          -- already linked (editorial filter)
      SELECT 1 FROM internal_links il
      WHERE il.source_article_id = a1.id
        AND il.target_article_id = a2.id
        AND il.is_active IS TRUE)
  AND NOT EXISTS (          -- non-expired decisions keep the pair blocked
      SELECT 1 FROM suggestions s
      WHERE s.source_article_id = a1.id
        AND s.target_article_id = a2.id
        AND s.status != 'expired')
ORDER BY e2.vector <=> e1.vector
LIMIT :k
""")

TARGET_SCORES_SQL = text("""
SELECT e2.article_id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
FROM embeddings e1
JOIN embeddings e2 ON e2.model = e1.model
WHERE e1.article_id = :article_id
  AND e1.model = :model
  AND e2.article_id IN :target_ids
""").bindparams(bindparam("target_ids", expanding=True))


def top_candidates(db: Session, article_id: int, model: str, k: int) -> list[tuple[int, float]]:
    rows = db.execute(TOP_K_SQL, {"article_id": article_id, "model": model, "k": k}).all()
    return [(r.target_id, float(r.score)) for r in rows]


def semantic_scores_for_targets(
    db: Session,
    *,
    article_id: int,
    model: str,
    target_ids: Sequence[int],
) -> dict[int, float]:
    """Return cosine scores for an explicit target set selected by another ranker."""
    if not target_ids:
        return {}
    rows = db.execute(
        TARGET_SCORES_SQL,
        {
            "article_id": article_id,
            "model": model,
            "target_ids": list(target_ids),
        },
    ).all()
    scores = {row.target_id: float(row.score) for row in rows}
    missing = set(target_ids) - set(scores)
    if missing:
        raise ValueError(
            f"missing {len(missing)} embedding score(s) for source article {article_id}"
        )
    return scores
