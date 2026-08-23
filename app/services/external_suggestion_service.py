"""Fallback-only persistence for dynamic external-search suggestions."""

import logging
import math
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.ml.external.base import (
    ExternalSearchError,
    ExternalSearchNotConfigured,
    ExternalSearchProvider,
    ExternalSearchResult,
)
from app.ml.external.cleaning import normalize_external_url
from app.ml.external.tavily import TAVILY_MAX_QUERY_CHARS, TavilySearchProvider
from app.models import (
    Article,
    Embedding,
    ExternalSearchAuditEvent,
    Site,
    Suggestion,
    SuggestionEvent,
)
from app.services.editorial_feedback import score_percent
from app.services.external_link_policy import (
    WebSearchSafetyEvaluation,
    evaluate_web_search_url,
    managed_domains,
    policy_state,
)

logger = logging.getLogger(__name__)

_MAX_STORED_TITLE_CHARS = 500
_MAX_STORED_SNIPPET_CHARS = 4_000


@dataclass(slots=True)
class ExternalFallbackResult:
    searched: int = 0
    created: int = 0
    credits_used: int = 0
    filtered: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class _ScorableCandidate:
    result: ExternalSearchResult
    normalized_url: str
    title: str
    snippet: str
    safety: WebSearchSafetyEvaluation
    provider_rank: int


def _audit_event(
    db: Session,
    *,
    site: Site,
    article: Article,
    job_run_id: int | None,
    provider: str,
    request_id: str | None,
    query: str,
    decision: str,
    candidate_url: str | None = None,
    provider_score: float | None = None,
    suggestion_id: int | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        ExternalSearchAuditEvent(
            site_id=site.id,
            source_article_id=article.id,
            suggestion_id=suggestion_id,
            job_run_id=job_run_id,
            provider=provider[:50],
            provider_request_id=request_id[:255] if request_id else None,
            search_query=query,
            candidate_url=candidate_url[:2048] if candidate_url else None,
            provider_score=provider_score,
            decision=decision[:30],
            details=details or {},
        )
    )


def _semantic_scores(
    db: Session,
    *,
    article: Article,
    model: str,
    candidates: list[_ScorableCandidate],
) -> list[float]:
    source_vector = db.scalar(
        select(Embedding.vector).where(
            Embedding.article_id == article.id,
            Embedding.model == model,
        )
    )
    if source_vector is None:
        raise ValueError(f"source article {article.id} has no {model!r} embedding")

    from app.ml.embeddings import encode  # lazy - heavy import

    inputs = [f"{candidate.title}\n{candidate.snippet}".strip() for candidate in candidates]
    vectors = list(encode(inputs))
    if len(vectors) != len(candidates):
        raise ValueError(
            f"Embedding configuration error for model {model!r}: produced "
            f"{len(vectors)} vectors for {len(candidates)} external candidates"
        )
    source_values = [float(value) for value in source_vector]
    scores: list[float] = []
    for vector in vectors:
        if len(vector) != len(source_values):
            raise ValueError(
                f"Embedding configuration error for model {model!r}: produced dimension "
                f"{len(vector)}, source dimension {len(source_values)}"
            )
        score = math.fsum(
            source_value * float(target_value)
            for source_value, target_value in zip(source_values, vector, strict=True)
        )
        scores.append(max(-1.0, min(1.0, score)))
    return scores


def fill_external_suggestion_gap(
    db: Session,
    *,
    site: Site,
    article: Article,
    missing_slots: int,
    model: str,
    job_run_id: int | None = None,
    provider: ExternalSearchProvider | None = None,
) -> ExternalFallbackResult:
    """Use one bounded web search only when the normal pipeline left open slots."""

    outcome = ExternalFallbackResult()
    if missing_slots <= 0:
        return outcome

    query = article.title.strip()[:TAVILY_MAX_QUERY_CHARS]
    if not query:
        outcome.filtered["empty_title"] += 1
        return outcome

    policy = policy_state(db, site.id)
    search_provider = provider or TavilySearchProvider()
    if not policy.external_links_enabled:
        # Before the per-candidate guard, not with it. The search is billed and
        # it sends this article's title to a third party — both happen the moment
        # the request leaves, and rejecting every result afterwards undoes
        # neither. A site with external links switched off must produce no
        # outbound request at all.
        outcome.filtered["external_links_disabled"] += 1
        _audit_event(
            db,
            site=site,
            article=article,
            job_run_id=job_run_id,
            provider=search_provider.name,
            request_id=None,
            query=query,
            decision="external_links_disabled",
            details={"missing_slots": missing_slots},
        )
        return outcome
    owned = managed_domains(db)
    exclude_domains = sorted(
        set(owned) | set(policy.blocklist_domains) | set(policy.competitor_domains)
    )
    try:
        response = search_provider.search(
            query,
            max_results=settings.tavily_max_results_per_request,
            exclude_domains=exclude_domains,
        )
    except ExternalSearchNotConfigured:
        outcome.filtered["not_configured"] += 1
        return outcome
    except ExternalSearchError as error:
        logger.warning(
            "external fallback failed for site %s source %s: %s",
            site.id,
            article.id,
            error,
        )
        outcome.searched = 1
        outcome.filtered["request_failed"] += 1
        _audit_event(
            db,
            site=site,
            article=article,
            job_run_id=job_run_id,
            provider=search_provider.name,
            request_id=None,
            query=query,
            decision="request_failed",
            details={"error_type": type(error).__name__, "message": str(error)[:500]},
        )
        return outcome

    outcome.searched = 1
    outcome.credits_used = response.credits_used or 0
    provider_name = response.provider or search_provider.name
    stored_provider_name = provider_name[:50]
    stored_request_id = response.request_id[:255] if response.request_id else None
    _audit_event(
        db,
        site=site,
        article=article,
        job_run_id=job_run_id,
        provider=provider_name,
        request_id=response.request_id,
        query=response.query,
        decision="request_completed",
        details={
            "attempt_count": response.attempt_count,
            "credits_used": response.credits_used,
            "exclude_domains": exclude_domains[:150],
            "response_time_seconds": response.response_time_seconds,
            "result_count": len(response.results),
        },
    )
    if not response.results:
        outcome.filtered["no_results"] += 1
        return outcome

    existing_urls = set(
        db.scalars(
            select(Suggestion.external_url).where(
                Suggestion.source_article_id == article.id,
                Suggestion.external_url.is_not(None),
                Suggestion.status != "expired",
            )
        )
    )
    seen_urls: set[str] = set()
    scorable: list[_ScorableCandidate] = []

    for provider_rank, result in enumerate(response.results, start=1):
        try:
            normalized_url = normalize_external_url(result.url)
        except ValueError as error:
            outcome.filtered["invalid_url"] += 1
            _audit_event(
                db,
                site=site,
                article=article,
                job_run_id=job_run_id,
                provider=provider_name,
                request_id=response.request_id,
                query=response.query,
                candidate_url=result.url,
                provider_score=result.provider_score,
                decision="invalid_url",
                details={"provider_rank": provider_rank, "reason": str(error)},
            )
            continue
        if len(normalized_url) > 2048:
            outcome.filtered["url_too_long"] += 1
            _audit_event(
                db,
                site=site,
                article=article,
                job_run_id=job_run_id,
                provider=provider_name,
                request_id=response.request_id,
                query=response.query,
                candidate_url=normalized_url,
                provider_score=result.provider_score,
                decision="url_too_long",
                details={"provider_rank": provider_rank},
            )
            continue

        safety = evaluate_web_search_url(
            source_site=site,
            target_url=normalized_url,
            policy=policy,
            owned_domains=owned,
        )
        if not safety.eligible:
            outcome.filtered["safety_blocked"] += 1
            _audit_event(
                db,
                site=site,
                article=article,
                job_run_id=job_run_id,
                provider=provider_name,
                request_id=response.request_id,
                query=response.query,
                candidate_url=normalized_url,
                provider_score=result.provider_score,
                decision="safety_blocked",
                details={
                    "provider_rank": provider_rank,
                    "safety": safety.as_score_component(),
                },
            )
            continue
        if normalized_url in existing_urls or normalized_url in seen_urls:
            outcome.filtered["duplicate"] += 1
            _audit_event(
                db,
                site=site,
                article=article,
                job_run_id=job_run_id,
                provider=provider_name,
                request_id=response.request_id,
                query=response.query,
                candidate_url=normalized_url,
                provider_score=result.provider_score,
                decision="duplicate",
                details={"provider_rank": provider_rank},
            )
            continue

        seen_urls.add(normalized_url)
        scorable.append(
            _ScorableCandidate(
                result=result,
                normalized_url=normalized_url,
                title=(result.title.strip() or safety.domain)[:_MAX_STORED_TITLE_CHARS],
                snippet=result.snippet.strip()[:_MAX_STORED_SNIPPET_CHARS],
                safety=safety,
                provider_rank=provider_rank,
            )
        )

    scores = (
        _semantic_scores(
            db,
            article=article,
            model=model,
            candidates=scorable,
        )
        if scorable
        else []
    )
    ranked = sorted(
        zip(scorable, scores, strict=True),
        key=lambda item: (-item[1], item[0].provider_rank),
    )
    selected = 0
    for candidate, semantic_score in ranked:
        details = {
            "provider_rank": candidate.provider_rank,
            "provider_result_id": candidate.result.provider_result_id,
            "semantic_model": model,
            "semantic_score": semantic_score,
            "safety": candidate.safety.as_score_component(),
        }
        if (
            semantic_score < settings.suggestion_min_score
            or score_percent(semantic_score) < site.editorial_min_score_percent
        ):
            decision = "below_threshold"
            outcome.filtered[decision] += 1
            _audit_event(
                db,
                site=site,
                article=article,
                job_run_id=job_run_id,
                provider=provider_name,
                request_id=response.request_id,
                query=response.query,
                candidate_url=candidate.normalized_url,
                provider_score=candidate.result.provider_score,
                decision=decision,
                details=details,
            )
            continue
        if selected >= missing_slots:
            decision = "capacity_not_selected"
            outcome.filtered[decision] += 1
            _audit_event(
                db,
                site=site,
                article=article,
                job_run_id=job_run_id,
                provider=provider_name,
                request_id=response.request_id,
                query=response.query,
                candidate_url=candidate.normalized_url,
                provider_score=candidate.result.provider_score,
                decision=decision,
                details=details,
            )
            continue

        feature_snapshot = {
            "semantic": semantic_score,
            "semantic_model": model,
            "provider_rank": candidate.provider_rank,
            "external_safety": candidate.safety.as_score_component(),
        }
        suggestion = Suggestion(
            site_id=site.id,
            source_article_id=article.id,
            generation_job_run_id=job_run_id,
            target_article_id=None,
            external_url=candidate.normalized_url,
            external_title=candidate.title,
            external_snippet=candidate.snippet,
            provider=stored_provider_name,
            provider_request_id=stored_request_id,
            provider_score=candidate.result.provider_score,
            search_query=response.query,
            method="external_search",
            score=semantic_score,
            # The provider's own relevance is not on the fusion's scale, so a
            # web-search row queues on the one bounded number it shares with
            # every other method.
            rank_score=semantic_score,
            score_components=feature_snapshot,
            retrieval_version="external_search_v1",
            ranking_version="external_search_provider_v1",
            final_rank=candidate.provider_rank,
            feature_snapshot=feature_snapshot,
            status="pending",
        )
        db.add(suggestion)
        db.flush()
        trace_details = {
            **details,
            "provider": stored_provider_name,
            "provider_request_id": stored_request_id,
            "provider_score": candidate.result.provider_score,
            "search_query": response.query,
            "normalized_url": candidate.normalized_url,
        }
        db.add(
            SuggestionEvent(
                suggestion_id=suggestion.id,
                event_type="external_discovered",
                actor="external-search",
                details=trace_details,
            )
        )
        _audit_event(
            db,
            site=site,
            article=article,
            job_run_id=job_run_id,
            provider=provider_name,
            request_id=response.request_id,
            query=response.query,
            candidate_url=candidate.normalized_url,
            provider_score=candidate.result.provider_score,
            suggestion_id=suggestion.id,
            decision="accepted",
            details=trace_details,
        )
        existing_urls.add(candidate.normalized_url)
        selected += 1
        outcome.created += 1

    return outcome
