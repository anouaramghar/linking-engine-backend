"""Cosine retrieval primitives over pgvector (exact below ~100k vectors).

Two families of query live here.

`top_candidates` is the long-standing cosine query retained for frozen offline
comparisons against the pre-global behavior.

The global Hybrid path uses `_HYBRID_ELIGIBILITY_SQL` instead. Dense retrieval applies it
while choosing its own top k; `eligible_candidate_scores` applies the identical
predicate to a candidate list produced elsewhere. That second use is the point.
BM25 ranks documents — it has no way to know about existing links, prior
editorial decisions, or a target whose vector is a near-duplicate of the source.
Keeping the rules in one string is what makes a lexical-only candidate obey
exactly the same rules as the dense candidates it competes with, rather than a
subset someone remembered to reimplement.
"""

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

# The Hybrid eligibility rules, in one place so both halves of the
# candidate union are filtered by the same predicate. Every clause is a rule the
# baseline path applies somewhere — the link, decision, active, site, and model
# rules in SQL; the fingerprint and title rules in the ranker's in-memory corpus
# — plus the near-duplicate vector ceiling, which only a vector comparison can
# express and which a lexical candidate would otherwise slip past entirely.
_HYBRID_ELIGIBILITY_SQL = """
FROM embeddings e1
JOIN articles a1 ON a1.id = e1.article_id
JOIN embeddings e2 ON e2.model = e1.model AND e2.article_id != e1.article_id
JOIN articles a2 ON a2.id = e2.article_id AND a2.site_id = a1.site_id
WHERE a1.id = :article_id
  AND e1.model = :model
  AND a2.is_active IS TRUE
  AND (                     -- identical inputs are duplicate pages, not link candidates
      e1.content_fingerprint IS NULL
      OR e2.content_fingerprint IS NULL
      OR e2.content_fingerprint <> e1.content_fingerprint)
  AND lower(btrim(a2.title)) <> lower(btrim(a1.title))
  AND (e2.vector <=> e1.vector) > :duplicate_distance
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
"""

HYBRID_TOP_K_SQL = text(f"""
SELECT a2.id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
{_HYBRID_ELIGIBILITY_SQL}
ORDER BY e2.vector <=> e1.vector, a2.id
LIMIT :k
""")

ELIGIBLE_SCORES_SQL = text(f"""
SELECT a2.id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
{_HYBRID_ELIGIBILITY_SQL}
  AND a2.id IN :target_ids
""").bindparams(bindparam("target_ids", expanding=True))

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
    """The legacy cosine query retained for frozen offline comparisons."""
    rows = db.execute(TOP_K_SQL, {"article_id": article_id, "model": model, "k": k}).all()
    return [(r.target_id, float(r.score)) for r in rows]


def eligible_top_candidates(
    db: Session,
    article_id: int,
    model: str,
    k: int,
    duplicate_similarity_threshold: float,
) -> list[tuple[int, float]]:
    """The Hybrid dense pool: the k closest targets that pass every rule."""
    rows = db.execute(
        HYBRID_TOP_K_SQL,
        {
            "article_id": article_id,
            "model": model,
            "k": k,
            "duplicate_distance": 1.0 - duplicate_similarity_threshold,
        },
    ).all()
    return [(r.target_id, float(r.score)) for r in rows]


def eligible_candidate_scores(
    db: Session,
    article_id: int,
    model: str,
    target_ids: Sequence[int],
    duplicate_similarity_threshold: float,
) -> dict[int, float]:
    """Filter a candidate list to the eligible targets, with each one's cosine score.

    Absence from the result is the answer for an ineligible candidate, so this
    doubles as the authoritative check on lexically-retrieved targets: one round
    trip returns both "may this pair be suggested" and "how similar is it".
    """
    if not target_ids:
        return {}
    rows = db.execute(
        ELIGIBLE_SCORES_SQL,
        {
            "article_id": article_id,
            "model": model,
            "target_ids": list(target_ids),
            "duplicate_distance": 1.0 - duplicate_similarity_threshold,
        },
    ).all()
    return {row.target_id: float(row.score) for row in rows}


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
