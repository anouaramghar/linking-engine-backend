"""Suggestion pipeline: encode missing embeddings, then cosine top-k -> pending suggestions
(sequence 4.2)."""

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import and_, func, select, update

from app.config import settings
from app.db import SessionLocal, engine
from app.models import Article, Embedding, Suggestion
from app.models.article import EMBEDDING_DIM
from app.ml.baseline import top_candidates
from app.ml.hybrid import HybridRanker
from app.services.job_service import record_progress

BATCH_SIZE = 32
INPUT_RECIPE_VERSION = 1
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
) -> int:
    """Encode active articles whose model-specific embedding is missing or stale."""
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
            encoded=encoded,
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


def _ranking_mode(site_id: int) -> str:
    if site_id in settings.v1_pilot_site_ids:
        return "pilot"
    if site_id in settings.v1_shadow_site_ids:
        return "shadow"
    return "baseline"


def _baseline_rows(db, article_id: int, model: str, remaining: int) -> list[tuple[int, float]]:
    return top_candidates(db, article_id, model, remaining)


def _evenly_spaced_ids(article_ids: list[int], maximum: int) -> set[int]:
    selected_count = min(maximum, len(article_ids))
    if not selected_count:
        return set()
    return {
        article_ids[index * len(article_ids) // selected_count] for index in range(selected_count)
    }


def generate_suggestions(site_id: int, job_run_id: int | None = None) -> dict:
    """RQ task body."""
    with _site_analysis_lock(site_id):
        db = SessionLocal()
        try:
            model = settings.embedding_model
            _validate_embedding_dimension(model)
            encoded = _embed_missing(db, site_id, model, job_run_id)
            ranking_mode = _ranking_mode(site_id)
            hybrid_ranker = None
            hybrid_load_failed = False
            if ranking_mode != "baseline":
                try:
                    hybrid_ranker = HybridRanker.load(
                        db,
                        site_id=site_id,
                        model=model,
                    )
                except Exception:
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
                _evenly_spaced_ids(article_ids, settings.v1_shadow_max_sources)
                if ranking_mode == "shadow"
                else set()
            )
            existing_counts = dict(
                db.execute(
                    select(Suggestion.source_article_id, func.count())
                    .where(
                        Suggestion.site_id == site_id,
                        Suggestion.status.in_(("pending", "approved", "applying")),
                    )
                    .group_by(Suggestion.source_article_id)
                ).all()
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
            for article_id in article_ids:
                remaining = settings.max_suggestions_per_article - existing_counts.get(
                    article_id, 0
                )
                has_capacity = remaining > 0
                shadow_selected = ranking_mode == "shadow" and article_id in shadow_source_ids
                if not has_capacity and not shadow_selected:
                    continue
                if has_capacity:
                    eligible_sources += 1
                method = "baseline_cosine"
                candidate_rows: list[tuple[int, float]]
                if ranking_mode == "baseline" or (ranking_mode == "shadow" and not shadow_selected):
                    candidate_rows = _baseline_rows(
                        db,
                        article_id,
                        model,
                        remaining,
                    )
                elif hybrid_ranker is None:
                    fallback_sources += 1
                    candidate_rows = (
                        _baseline_rows(
                            db,
                            article_id,
                            model,
                            remaining,
                        )
                        if has_capacity
                        else []
                    )
                else:
                    try:
                        ranking_limit = (
                            settings.max_suggestions_per_article if shadow_selected else remaining
                        )
                        ranking = hybrid_ranker.rank(
                            db,
                            source_id=article_id,
                            model=model,
                            limit=ranking_limit,
                        )
                        hybrid_sources += 1
                        dense_candidates_total += ranking.dense_count
                        lexical_candidates_total += ranking.lexical_count
                        union_candidates_total += ranking.union_count
                        hybrid_rows = [
                            (candidate.target_id, candidate.semantic_score)
                            for candidate in ranking.candidates
                        ]
                        if ranking_mode == "pilot":
                            candidate_rows = hybrid_rows
                            method = "hybrid_bm25"
                        else:
                            baseline_rows = [
                                (candidate.target_id, candidate.semantic_score)
                                for candidate in ranking.baseline_candidates[:ranking_limit]
                            ]
                            baseline_ids = [target_id for target_id, _score in baseline_rows]
                            hybrid_ids = [target_id for target_id, _score in hybrid_rows]
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
                                remaining,
                            )
                            if has_capacity
                            else []
                        )

                for target_id, score in candidate_rows:
                    db.add(
                        Suggestion(
                            site_id=site_id,
                            source_article_id=article_id,
                            target_article_id=target_id,
                            method=method,
                            score=score,
                            status="pending",
                        )
                    )
                    created += 1
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
            }
            if ranking_mode != "baseline":
                result.update(
                    {
                        "ranking_mode": ranking_mode,
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
