"""Baseline v1: cosine top-k over pgvector, exact search (A13 — no index below ~100k vectors).

Two families of query live here.

`top_candidates` is retained for explicit Hybrid comparisons and ranking fallback.

The Hybrid path uses `_PILOT_ELIGIBILITY_SQL` instead. Dense retrieval applies it
while choosing its own top k; `eligible_candidate_scores` applies the identical
predicate to a candidate list produced elsewhere. That second use is the point.
BM25 ranks documents — it has no way to know about existing links, prior
editorial decisions, or a target whose vector is a near-duplicate of the source.
Keeping the rules in one string is what makes a lexical-only candidate obey
exactly the same rules as the dense candidates it competes with, rather than a
subset someone remembered to reimplement.
"""

import re
from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config import settings

# Navigational and transactional targets, from settings rather than literals so
# a non-English site can supply its own without a code change or a migration.
# Both halves are bound parameters: the terms are operator-supplied, and the URL
# rule is a regex, so neither may be pasted into the statement text. An empty
# list disables its half — `<> ALL('{}')` is already true for every row, and the
# empty-pattern test short-circuits before the match.
_LOW_VALUE_TARGET_SQL = """
  AND lower(btrim(a2.title)) <> ALL(CAST(:low_value_titles AS text[]))
  AND (:low_value_url_pattern = '' OR lower(a2.url) !~ :low_value_url_pattern)
"""

_EXTERNAL_POLICY_SQL = """
  AND (
      :restrict_target_ids IS FALSE
      OR a2.id = ANY(CAST(:allowed_target_ids AS integer[])))
"""


def low_value_target_params() -> dict[str, object]:
    """The bound values behind `_LOW_VALUE_TARGET_SQL`.

    The slugs become one alternation rather than one regex per slug: this runs
    on the whole embedding join, before the top-k limit, so the row cost of the
    rule matters. Each slug is escaped — a term like `c++` is a literal here,
    not a quantifier that would fail the match or the parse.
    """
    slugs = "|".join(
        re.escape(slug.strip().lower())
        for slug in settings.low_value_target_url_slugs
        if slug.strip()
    )
    titles = [title.strip().lower() for title in settings.low_value_target_titles]
    return {
        "low_value_titles": titles,
        "low_value_url_pattern": rf"(^|/)({slugs})(/|[?#]|$)" if slugs else "",
    }


TOP_K_SQL = text(f"""
SELECT a2.id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
FROM embeddings e1
JOIN articles a1 ON a1.id = e1.article_id
JOIN embeddings e2 ON e2.model = e1.model AND e2.article_id != e1.article_id
JOIN articles a2 ON a2.id = e2.article_id
JOIN sites candidate_site ON candidate_site.id = a2.site_id
WHERE a1.id = :article_id
  AND e1.model = :model
  AND (
      a2.site_id = a1.site_id
      OR (
          candidate_site.platform = 'pool'
          AND candidate_site.pool_source_approved IS TRUE
          AND candidate_site.pool_source_quarantined IS FALSE))
  AND a2.is_active IS TRUE
  {_EXTERNAL_POLICY_SQL}
  AND (1 - (e2.vector <=> e1.vector)) >= :minimum_score
  {_LOW_VALUE_TARGET_SQL}
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
  AND NOT EXISTS (          -- the reverse direction is already proposed or a link
      SELECT 1 FROM suggestions s
      WHERE s.source_article_id = a2.id
        AND s.target_article_id = a1.id
        AND s.status IN ('pending', 'approved', 'applying', 'applied'))
ORDER BY e2.vector <=> e1.vector
LIMIT :k
""")

# Hybrid's eligibility rules, in one place so both halves of the ranking
# candidate union are filtered by the same predicate. Every clause is a rule the
# baseline path applies somewhere — the link, decision, active, site, and model
# rules in SQL; the fingerprint and title rules in the ranker's in-memory corpus
# — plus the near-duplicate vector ceiling, which only a vector comparison can
# express and which a lexical candidate would otherwise slip past entirely.
#
# The reverse-direction rule keeps A->B and B->A from both being proposed. The
# first direction generated wins an otherwise symmetric choice; later sources
# continue to their next eligible target instead of creating the mirror row.
_PILOT_ELIGIBILITY_SQL = f"""
FROM embeddings e1
JOIN articles a1 ON a1.id = e1.article_id
JOIN embeddings e2 ON e2.model = e1.model AND e2.article_id != e1.article_id
JOIN articles a2 ON a2.id = e2.article_id
JOIN sites candidate_site ON candidate_site.id = a2.site_id
WHERE a1.id = :article_id
  AND e1.model = :model
  AND (
      a2.site_id = a1.site_id
      OR (
          candidate_site.platform = 'pool'
          AND candidate_site.pool_source_approved IS TRUE
          AND candidate_site.pool_source_quarantined IS FALSE))
  AND a2.is_active IS TRUE
  {_EXTERNAL_POLICY_SQL}
  {_LOW_VALUE_TARGET_SQL}
  AND (                     -- identical inputs are duplicate pages, not link candidates
      e1.content_fingerprint IS NULL
      OR e2.content_fingerprint IS NULL
      OR e2.content_fingerprint <> e1.content_fingerprint)
  AND lower(btrim(a2.title)) <> lower(btrim(a1.title))
  AND (e2.vector <=> e1.vector) > :duplicate_distance
  AND (1 - (e2.vector <=> e1.vector)) >= :minimum_score
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
  AND NOT EXISTS (          -- the reverse direction is already proposed or a link
      SELECT 1 FROM suggestions s
      WHERE s.source_article_id = a2.id
        AND s.target_article_id = a1.id
        AND s.status IN ('pending', 'approved', 'applying', 'applied'))
"""

PILOT_TOP_K_SQL = text(f"""
SELECT a2.id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
{_PILOT_ELIGIBILITY_SQL}
ORDER BY e2.vector <=> e1.vector, a2.id
LIMIT :k
""")

ELIGIBLE_SCORES_SQL = text(f"""
SELECT a2.id AS target_id,
       1 - (e2.vector <=> e1.vector) AS score
{_PILOT_ELIGIBILITY_SQL}
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


def _external_policy_params(allowed_target_ids: Sequence[int] | None) -> dict[str, object]:
    return {
        "restrict_target_ids": allowed_target_ids is not None,
        "allowed_target_ids": list(allowed_target_ids or ()),
    }


def top_candidates(
    db: Session,
    article_id: int,
    model: str,
    k: int,
    *,
    allowed_target_ids: Sequence[int] | None = None,
) -> list[tuple[int, float]]:
    """The baseline query used by explicit comparisons and fallback."""
    rows = db.execute(
        TOP_K_SQL,
        {
            "article_id": article_id,
            "model": model,
            "k": k,
            "minimum_score": settings.suggestion_min_score,
            **_external_policy_params(allowed_target_ids),
            **low_value_target_params(),
        },
    ).all()
    return [(r.target_id, float(r.score)) for r in rows]


def eligible_top_candidates(
    db: Session,
    article_id: int,
    model: str,
    k: int,
    duplicate_similarity_threshold: float,
    *,
    allowed_target_ids: Sequence[int] | None = None,
) -> list[tuple[int, float]]:
    """The Hybrid dense pool: closest targets that pass every Hybrid rule."""
    rows = db.execute(
        PILOT_TOP_K_SQL,
        {
            "article_id": article_id,
            "model": model,
            "k": k,
            "duplicate_distance": 1.0 - duplicate_similarity_threshold,
            "minimum_score": settings.suggestion_min_score,
            **_external_policy_params(allowed_target_ids),
            **low_value_target_params(),
        },
    ).all()
    return [(r.target_id, float(r.score)) for r in rows]


def eligible_candidate_scores(
    db: Session,
    article_id: int,
    model: str,
    target_ids: Sequence[int],
    duplicate_similarity_threshold: float,
    *,
    allowed_target_ids: Sequence[int] | None = None,
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
            "minimum_score": settings.suggestion_min_score,
            **_external_policy_params(allowed_target_ids),
            **low_value_target_params(),
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
