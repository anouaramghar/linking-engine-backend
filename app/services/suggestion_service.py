"""Suggestion pipeline: encode missing embeddings, then cosine top-k -> pending suggestions
(sequence 4.2)."""

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import and_, func, select, update

from app.config import settings
from app.db import SessionLocal, engine
from app.models import Article, Embedding, Site, Suggestion
from app.models.article import EMBEDDING_DIM
from app.ml.baseline import top_candidates
from app.ml.hybrid import HybridRanker, RankedCandidate
from app.services.job_service import record_progress
from app.services.external_link_policy import external_target_context
from app.services.editorial_feedback import (
    FEEDBACK_CANDIDATE_POOL,
    load_editorial_feedback,
    rerank_with_editorial_feedback,
    score_percent,
)

BATCH_SIZE = 32
INPUT_RECIPE_VERSION = 1
# Open review work. 'applied' is deliberately absent: a published link stops
# occupying the queue. It still counts against the article — see _LIFETIME_STATUSES.
_ACTIVE_STATUSES = ("pending", "approved", "applying")
# Every row that is, or is on its way to becoming, a link on the source article.
_LIFETIME_STATUSES = (*_ACTIVE_STATUSES, "applied")
_ANALYSIS_LOCK_NAMESPACE = 0x4C4D
_DIMENSION_PROBE_INPUT = "LinkMesh dimension probe"
logger = logging.getLogger(__name__)


@contextmanager
def _site_analysis_lock(site_id: int) -> Iterator[None]:
    # Batch commits use a separate work session. This dedicated transaction therefore holds
    # the lock for the entire analysis and lets PostgreSQL release it reliably on every exit.
    with engine.begin() as lock_connection:
        lock_connection.execute(
            select(func.pg_advisory_xact_lock(_ANALYSIS_LOCK_NAMESPACE, site_id))
        ).scalar_one()
        yield


def _embed_missing(
    db,
    site_id: int,
    model: str,
    job_run_id: int | None = None,
    encoded_offset: int = 0,
) -> int:
    """Encode active articles whose model-specific embedding is missing or stale."""
    active_count = db.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.site_id == site_id, Article.is_active.is_(True))
    )
    if active_count > settings.analysis_max_articles_per_site:
        raise ValueError(
            f"analysis article count exceeded {settings.analysis_max_articles_per_site} "
            f"for site {site_id}"
        )

    encoded = 0
    last_article_id = 0
    while True:
        rows = db.execute(
            select(
                Article.id,
                Article.title,
                Article.content_text,
                Embedding.id,
                Embedding.content_fingerprint,
                Embedding.input_recipe_version,
                Embedding.vector_size,
            )
            .outerjoin(
                Embedding,
                and_(Embedding.article_id == Article.id, Embedding.model == model),
            )
            .where(
                Article.site_id == site_id,
                Article.is_active.is_(True),
                Article.id > last_article_id,
            )
            .order_by(Article.id)
            .limit(BATCH_SIZE)
        ).all()
        if not rows:
            return encoded
        last_article_id = rows[-1][0]
        batch = []
        for (
            article_id,
            title,
            text,
            embedding_id,
            stored_fingerprint,
            stored_recipe_version,
            stored_vector_size,
        ) in rows:
            if len(text) > settings.crawl_max_article_chars:
                raise ValueError(
                    f"article content exceeded {settings.crawl_max_article_chars} characters"
                )
            encode_input = f"{title}\n{text}"
            fingerprint = hashlib.sha256(encode_input.encode()).hexdigest()
            if (
                embedding_id is None
                or stored_fingerprint != fingerprint
                or stored_recipe_version != INPUT_RECIPE_VERSION
                or stored_vector_size != EMBEDDING_DIM
            ):
                batch.append((article_id, encode_input, fingerprint, embedding_id))
        if not batch:
            continue
        from app.ml.embeddings import encode  # lazy — heavy import

        vectors = list(encode([encode_input for _, encode_input, _, _ in batch]))
        if len(vectors) != len(batch):
            raise ValueError(
                f"Embedding configuration error for model {model!r}: produced "
                f"{len(vectors)} vectors for {len(batch)} inputs"
            )
        for vector in vectors:
            produced_size = len(vector)
            if produced_size != EMBEDDING_DIM:
                raise ValueError(
                    f"Embedding configuration error for model {model!r}: produced dimension "
                    f"{produced_size}, storage dimension {EMBEDDING_DIM}"
                )

        for (article_id, _, fingerprint, embedding_id), vector in zip(batch, vectors):
            values = {
                "vector": vector,
                "content_fingerprint": fingerprint,
                "input_recipe_version": INPUT_RECIPE_VERSION,
                "vector_size": len(vector),
            }
            if embedding_id is None:
                db.add(Embedding(article_id=article_id, model=model, **values))
            else:
                db.execute(update(Embedding).where(Embedding.id == embedding_id).values(**values))
        encoded += len(batch)
        record_progress(
            db,
            job_run_id,
            stage="encoding",
            encoded=encoded_offset + encoded,
        )
        db.commit()


def _validate_embedding_dimension(model: str) -> None:
    from app.ml.embeddings import encode  # lazy - heavy import

    vectors = list(encode([_DIMENSION_PROBE_INPUT]))
    if len(vectors) != 1:
        raise ValueError(
            f"Embedding configuration error for model {model!r}: produced "
            f"{len(vectors)} vectors for one probe input"
        )
    produced_size = len(vectors[0])
    if produced_size != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding configuration error for model {model!r}: produced dimension "
            f"{produced_size}, storage dimension {EMBEDDING_DIM}"
        )


def _ranking_mode(
    site_id: int,
    configured_mode: str,
    ranking_mode_override: str | None = None,
) -> str:
    if ranking_mode_override is not None:
        if ranking_mode_override not in {"baseline", "shadow", "hybrid"}:
            raise ValueError(f"unsupported ranking mode override: {ranking_mode_override}")
        return ranking_mode_override
    # Hybrid is the product default. `configured_mode` remains in the signature
    # for rolling API compatibility; only explicit comparison overrides differ.
    return "hybrid"


def _baseline_rows(
    db,
    article_id: int,
    model: str,
    remaining: int,
    *,
    allowed_target_ids: set[int] | None = None,
) -> list[RankedCandidate]:
    """The unchanged cosine path, in the shape the persistence loop expects.

    Baseline rows carry no score components: `score` is cosine similarity and
    that is the entire explanation, so an empty component blob would only add
    noise to the queue payload.
    """
    return [
        RankedCandidate(target_id=target_id, semantic_score=score)
        for target_id, score in top_candidates(
            db,
            article_id,
            model,
            remaining,
            allowed_target_ids=allowed_target_ids,
        )
    ]


def _evenly_spaced_ids(article_ids: list[int], maximum: int) -> set[int]:
    selected_count = min(maximum, len(article_ids))
    if not selected_count:
        return set()
    return {
        article_ids[index * len(article_ids) // selected_count] for index in range(selected_count)
    }


def generate_suggestions(
    site_id: int,
    job_run_id: int | None = None,
    *,
    ranking_mode_override: str | None = None,
    comparison_only: bool = False,
) -> dict:
    """RQ task body."""
    with _site_analysis_lock(site_id):
        db = SessionLocal()
        try:
            site = db.get(Site, site_id)
            if site is None:
                raise ValueError(f"site {site_id} not found")
            if site.platform == "pool":
                raise ValueError("content-pool sources cannot generate suggestions")
            allowed_target_ids, external_trust = external_target_context(db, site)
            model = settings.embedding_model
            _validate_embedding_dimension(model)
            encoded = _embed_missing(db, site_id, model, job_run_id)
            pool_site_ids = db.scalars(
                select(Article.site_id)
                .where(
                    Article.id.in_(allowed_target_ids),
                    Article.site_id != site_id,
                )
                .distinct()
                .order_by(Article.site_id)
            ).all()
            for pool_site_id in pool_site_ids:
                # Different customer analyses may share the same pool. Reuse the
                # analysis advisory lock so they cannot both insert one missing
                # article/model embedding at the same time.
                with _site_analysis_lock(pool_site_id):
                    encoded += _embed_missing(
                        db,
                        pool_site_id,
                        model,
                        job_run_id,
                        encoded_offset=encoded,
                    )
            ranking_mode = _ranking_mode(
                site_id,
                site.suggestion_mode,
                ranking_mode_override,
            )
            feedback_profile = load_editorial_feedback(db, site)
            if comparison_only and ranking_mode != "shadow":
                raise ValueError("comparison-only analysis requires shadow ranking")
            suggestion_cap = settings.hybrid_max_suggestions_per_article
            lifetime_cap = settings.hybrid_max_lifetime_links_per_article
            hybrid_ranker = None
            hybrid_load_failed = False
            if ranking_mode != "baseline":
                try:
                    hybrid_ranker = HybridRanker.load(
                        db,
                        site_id=site_id,
                        model=model,
                        allowed_target_ids=allowed_target_ids,
                    )
                except Exception:
                    # A PostgreSQL statement error leaves the transaction aborted.
                    # End that failed read transaction before the baseline path
                    # issues its next query. Hybrid loading happens after
                    # `_embed_missing` has committed, so there are no application
                    # writes here for the rollback to discard.
                    db.rollback()
                    hybrid_load_failed = True
                    logger.exception(
                        "hybrid ranker initialization failed for site %s; using baseline cosine",
                        site_id,
                    )

            article_ids = db.scalars(
                select(Article.id)
                .where(
                    Article.site_id == site_id,
                    Article.is_active.is_(True),
                )
                .order_by(Article.id)
            ).all()
            shadow_source_ids = (
                _evenly_spaced_ids(article_ids, settings.hybrid_max_sources_per_run)
                if ranking_mode == "shadow"
                else set()
            )
            # One pass, two bounds per source: what is still in review, and what
            # this article has ever been given. They differ only by 'applied' —
            # a link that is on the page now and does not stop being one because
            # it left the queue.
            source_counts = db.execute(
                select(
                    Suggestion.source_article_id,
                    func.count().filter(Suggestion.status.in_(_ACTIVE_STATUSES)),
                    func.count(),
                )
                .where(
                    Suggestion.site_id == site_id,
                    Suggestion.status.in_(_LIFETIME_STATUSES),
                )
                .group_by(Suggestion.source_article_id)
            ).all()
            existing_counts = {source_id: active for source_id, active, _ in source_counts}
            lifetime_counts = {source_id: lifetime for source_id, _, lifetime in source_counts}
            active_count = sum(existing_counts.values())
            site_capacity = max(
                0,
                min(
                    len(article_ids) * suggestion_cap,
                    settings.hybrid_max_active_suggestions_per_site,
                )
                - active_count,
            )
            created = 0
            eligible_sources = 0
            hybrid_sources = 0
            fallback_sources = 0
            dense_candidates_total = 0
            lexical_candidates_total = 0
            union_candidates_total = 0
            shadow_overlap_total = 0.0
            shadow_exact_matches = 0
            hybrid_sources_selected = 0
            for article_id in article_ids:
                if ranking_mode == "hybrid" and site_capacity <= 0:
                    break
                remaining = min(
                    suggestion_cap - existing_counts.get(article_id, 0),
                    lifetime_cap - lifetime_counts.get(article_id, 0),
                    site_capacity,
                )
                has_capacity = remaining > 0
                shadow_selected = ranking_mode == "shadow" and article_id in shadow_source_ids
                if comparison_only and not shadow_selected:
                    continue
                if not has_capacity and not shadow_selected:
                    continue
                if ranking_mode == "hybrid" and has_capacity:
                    if hybrid_sources_selected >= settings.hybrid_max_sources_per_run:
                        break
                    hybrid_sources_selected += 1
                if has_capacity:
                    eligible_sources += 1
                method = "baseline_cosine"
                candidate_rows: list[RankedCandidate]
                candidate_pool_limit = (
                    max(remaining, FEEDBACK_CANDIDATE_POOL)
                    if feedback_profile is not None and has_capacity and not comparison_only
                    else remaining
                )
                if ranking_mode == "baseline" or (ranking_mode == "shadow" and not shadow_selected):
                    candidate_rows = _baseline_rows(
                        db,
                        article_id,
                        model,
                        candidate_pool_limit,
                        allowed_target_ids=allowed_target_ids,
                    )
                elif hybrid_ranker is None:
                    fallback_sources += 1
                    candidate_rows = (
                        _baseline_rows(
                            db,
                            article_id,
                            model,
                            candidate_pool_limit,
                            allowed_target_ids=allowed_target_ids,
                        )
                        if has_capacity
                        else []
                    )
                else:
                    try:
                        ranking_limit = (
                            suggestion_cap if shadow_selected else candidate_pool_limit
                        )
                        ranking = hybrid_ranker.rank(
                            db,
                            source_id=article_id,
                            model=model,
                            limit=ranking_limit,
                            duplicate_similarity_threshold=(
                                settings.suggestion_duplicate_similarity_threshold
                            ),
                            # Only the shadow comparison needs the baseline rows,
                            # and fetching them costs a second dense query.
                            include_baseline=ranking_mode == "shadow",
                        )
                        hybrid_sources += 1
                        dense_candidates_total += ranking.dense_count
                        lexical_candidates_total += ranking.lexical_count
                        union_candidates_total += ranking.union_count
                        hybrid_rows = list(ranking.candidates)
                        if ranking_mode == "hybrid":
                            candidate_rows = hybrid_rows
                            method = "hybrid_bm25"
                        else:
                            baseline_rows = list(ranking.baseline_candidates[:ranking_limit])
                            baseline_ids = [candidate.target_id for candidate in baseline_rows]
                            hybrid_ids = [candidate.target_id for candidate in hybrid_rows]
                            denominator = max(
                                1,
                                min(
                                    ranking_limit,
                                    len(set(baseline_ids) | set(hybrid_ids)),
                                ),
                            )
                            shadow_overlap_total += (
                                len(set(baseline_ids) & set(hybrid_ids)) / denominator
                            )
                            shadow_exact_matches += baseline_ids == hybrid_ids
                            candidate_rows = baseline_rows[:remaining] if has_capacity else []
                    except Exception:
                        # Ranking runs before this source adds suggestions or
                        # progress. Rolling back is therefore safe, and required
                        # when the caught failure came from PostgreSQL: without it
                        # the baseline query fails with InFailedSqlTransaction.
                        db.rollback()
                        fallback_sources += 1
                        logger.exception(
                            "hybrid ranking failed for site %s source %s; using baseline cosine",
                            site_id,
                            article_id,
                        )
                        candidate_rows = (
                            _baseline_rows(
                                db,
                                article_id,
                                model,
                                candidate_pool_limit,
                                allowed_target_ids=allowed_target_ids,
                            )
                            if has_capacity
                            else []
                        )

                feedback_components: dict[int, dict] = {}
                if has_capacity and not comparison_only:
                    candidate_rows = [
                        candidate
                        for candidate in candidate_rows
                        if score_percent(candidate.semantic_score)
                        >= site.editorial_min_score_percent
                    ]
                    if feedback_profile is not None:
                        candidate_rows, feedback_components = rerank_with_editorial_feedback(
                            candidate_rows,
                            feedback_profile,
                            weight=site.editorial_feedback_weight,
                        )
                    candidate_rows = candidate_rows[:remaining]
                if comparison_only:
                    candidate_rows = []
                for candidate in candidate_rows:
                    db.add(
                        Suggestion(
                            site_id=site_id,
                            source_article_id=article_id,
                            target_article_id=candidate.target_id,
                            method=method,
                            # Cosine similarity for both methods, so one number keeps
                            # one meaning across the mixed queue.
                            score=candidate.semantic_score,
                            score_components=(
                                {
                                    **(
                                        candidate.score_components()
                                        if method == "hybrid_bm25"
                                        else {}
                                    ),
                                    **(
                                        {
                                            "external_trust": external_trust[
                                                candidate.target_id
                                            ].as_score_component()
                                        }
                                        if candidate.target_id in external_trust
                                        else {}
                                    ),
                                    **(
                                        {
                                            "editorial_feedback": feedback_components[
                                                candidate.target_id
                                            ]
                                        }
                                        if candidate.target_id in feedback_components
                                        else {}
                                    ),
                                }
                                or None
                            ),
                            status="pending",
                        )
                    )
                    created += 1
                site_capacity -= len(candidate_rows)
                record_progress(
                    db,
                    job_run_id,
                    stage="suggesting",
                    created=created,
                    ranking_mode=ranking_mode,
                    hybrid_fallback_sources=fallback_sources,
                )
                db.commit()
            result = {
                "articles_encoded": encoded,
                "suggestions_created": created,
                "external_candidates_eligible": sum(
                    evaluation.eligible for evaluation in external_trust.values()
                ),
                "external_candidates_blocked": sum(
                    not evaluation.eligible for evaluation in external_trust.values()
                ),
                "editorial_feedback_applied": feedback_profile is not None,
                "editorial_feedback_samples": (
                    feedback_profile.samples if feedback_profile is not None else 0
                ),
                "editorial_min_score_percent": site.editorial_min_score_percent,
            }
            if ranking_mode != "baseline":
                result.update(
                    {
                        "ranking_mode": ranking_mode,
                        "comparison_only": comparison_only,
                        "suggestion_cap_per_source": suggestion_cap,
                        "source_limit_per_run": (settings.hybrid_max_sources_per_run),
                        "sources_selected": (
                            hybrid_sources_selected
                            if ranking_mode == "hybrid"
                            else len(shadow_source_ids)
                        ),
                        "hybrid_ranker_loaded": not hybrid_load_failed,
                        "eligible_sources": eligible_sources,
                        "shadow_sources_selected": len(shadow_source_ids),
                        "hybrid_sources_evaluated": hybrid_sources,
                        "hybrid_fallback_sources": fallback_sources,
                        "mean_dense_candidates": (
                            round(dense_candidates_total / hybrid_sources, 2)
                            if hybrid_sources
                            else 0.0
                        ),
                        "mean_lexical_candidates": (
                            round(lexical_candidates_total / hybrid_sources, 2)
                            if hybrid_sources
                            else 0.0
                        ),
                        "mean_union_candidates": (
                            round(union_candidates_total / hybrid_sources, 2)
                            if hybrid_sources
                            else 0.0
                        ),
                    }
                )
                if ranking_mode == "shadow":
                    result.update(
                        {
                            "shadow_mean_overlap_at_5": (
                                round(shadow_overlap_total / hybrid_sources, 4)
                                if hybrid_sources
                                else 0.0
                            ),
                            "shadow_exact_order_rate": (
                                round(shadow_exact_matches / hybrid_sources, 4)
                                if hybrid_sources
                                else 0.0
                            ),
                        }
                    )
            return result
        finally:
            db.close()
