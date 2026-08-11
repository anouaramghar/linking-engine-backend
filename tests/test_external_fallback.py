import math
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import settings
from app.domain_policy import domain_from_url
from app.ml.external.base import ExternalSearchResponse, ExternalSearchResult
from app.models import Article, Embedding, ExternalSearchAuditEvent, Suggestion, SuggestionEvent
from app.models.article import EMBEDDING_DIM
from app.services.external_suggestion_service import fill_external_suggestion_gap


def _vector(similarity: float = 1.0, axis: int = 1) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    if similarity < 1.0:
        vector[axis] = math.sqrt(1.0 - similarity**2)
    return vector


def _source(db, site) -> Article:
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/fallback-source",
        title="Reliable tomato canning guide",
        content_text="Safe temperatures and processing times for preserved tomatoes.",
    )
    db.add(article)
    db.flush()
    db.add(
        Embedding(
            article_id=article.id,
            model=settings.embedding_model,
            vector=_vector(),
            vector_size=EMBEDDING_DIM,
        )
    )
    db.commit()
    return article


@dataclass
class FakeProvider:
    response: ExternalSearchResponse
    name: str = "tavily"
    calls: list[tuple[str, int, tuple[str, ...]]] = field(default_factory=list)

    def search(
        self,
        query: str,
        *,
        max_results: int,
        exclude_domains=(),
    ) -> ExternalSearchResponse:
        self.calls.append((query, max_results, tuple(exclude_domains)))
        return self.response


def test_fallback_filters_scores_persists_and_audits(monkeypatch, db, site) -> None:
    source = _source(db, site)
    provider = FakeProvider(
        ExternalSearchResponse(
            provider="tavily",
            query=source.title,
            request_id="request-1",
            credits_used=1,
            attempt_count=2,
            results=(
                ExternalSearchResult(
                    title="Unsafe",
                    url="http://unsafe.example/guide",
                    provider_score=0.99,
                ),
                ExternalSearchResult(
                    title="Canning reference",
                    url="https://reference.example/guide?utm_source=test",
                    snippet="Research-backed tomato canning instructions.",
                    provider_score=0.91,
                    provider_result_id="result-a",
                ),
                ExternalSearchResult(
                    title="Duplicate",
                    url="https://reference.example/guide",
                    provider_score=0.90,
                ),
                ExternalSearchResult(
                    title="Second reference",
                    url="https://second.example/guide",
                    snippet="More safe processing instructions.",
                    provider_score=0.85,
                ),
            ),
        )
    )
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(0.80, index + 2) for index, _text in enumerate(texts)],
    )

    outcome = fill_external_suggestion_gap(
        db,
        site=site,
        article=source,
        missing_slots=1,
        model=settings.embedding_model,
        provider=provider,
    )
    db.commit()

    assert len(provider.calls) == 1
    assert provider.calls[0][:2] == (source.title, 5)
    assert domain_from_url(site.base_url) in provider.calls[0][2]
    assert outcome.created == 1
    assert outcome.credits_used == 1
    assert outcome.filtered == {
        "safety_blocked": 1,
        "duplicate": 1,
        "capacity_not_selected": 1,
    }
    suggestion = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source.id)
    ).one()
    assert suggestion.external_url == "https://reference.example/guide"
    assert suggestion.target_article_id is None
    assert suggestion.provider_score == 0.91
    assert suggestion.score == 0.80
    assert suggestion.score_components["external_safety"]["eligible"] is True

    decisions = db.scalars(
        select(ExternalSearchAuditEvent.decision)
        .where(ExternalSearchAuditEvent.source_article_id == source.id)
        .order_by(ExternalSearchAuditEvent.id)
    ).all()
    assert decisions == [
        "request_completed",
        "safety_blocked",
        "duplicate",
        "accepted",
        "capacity_not_selected",
    ]
    trace = db.scalars(
        select(SuggestionEvent).where(
            SuggestionEvent.suggestion_id == suggestion.id,
            SuggestionEvent.event_type == "external_discovered",
        )
    ).one()
    assert trace.details["provider_request_id"] == "request-1"
    assert trace.details["semantic_model"] == settings.embedding_model
    request_event = db.scalars(
        select(ExternalSearchAuditEvent).where(
            ExternalSearchAuditEvent.source_article_id == source.id,
            ExternalSearchAuditEvent.decision == "request_completed",
        )
    ).one()
    assert request_event.details["credits_used"] == 1
    assert request_event.details["attempt_count"] == 2


def test_zero_gap_never_calls_provider(db, site) -> None:
    source = _source(db, site)
    provider = FakeProvider(
        ExternalSearchResponse(provider="tavily", query=source.title, results=())
    )

    outcome = fill_external_suggestion_gap(
        db,
        site=site,
        article=source,
        missing_slots=0,
        model=settings.embedding_model,
        provider=provider,
    )

    assert provider.calls == []
    assert outcome.searched == 0
