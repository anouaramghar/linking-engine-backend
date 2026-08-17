from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models import Article, Suggestion, SuggestionEvent


def _article(db, site, title: str) -> Article:
    article = Article(
        site_id=site.id,
        url=f"https://example.com/{title}",
        title=title,
        content_text=f"Content for {title}",
        is_active=True,
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db.add(article)
    db.flush()
    return article


def _suggestion(db, site, source: Article, target: Article, **overrides) -> Suggestion:
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        status="pending",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        **overrides,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def test_exposure_is_recorded_once_and_review_keeps_label_context(client, db, site):
    source = _article(db, site, "source")
    target = _article(db, site, "target")
    suggestion = _suggestion(db, site, source, target)

    first = client.post(
        "/api/v1/suggestions/exposure",
        json={"suggestion_ids": [suggestion.id], "surface": "queue"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"exposed": 1}

    second = client.post(
        "/api/v1/suggestions/exposure",
        json={"suggestion_ids": [suggestion.id], "surface": "preview"},
    )
    assert second.status_code == 200, second.text

    reviewed = client.put(
        f"/api/v1/suggestions/{suggestion.id}",
        json={"status": "rejected", "rejection_reason": "wrong_target"},
    )
    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["shown_at"] is not None
    assert body["exposure_count"] == 2
    assert body["reviewer_id"]
    assert body["rejection_reason"] == "wrong_target"

    db.expire_all()
    row = db.get(Suggestion, suggestion.id)
    assert row is not None
    assert row.exposure_count == 2
    events = list(
        db.scalars(
            select(SuggestionEvent)
            .where(SuggestionEvent.suggestion_id == suggestion.id)
            .order_by(SuggestionEvent.id)
        )
    )
    exposed = [event for event in events if event.event_type == "exposed"]
    reviewed_events = [event for event in events if event.event_type == "reviewed"]
    assert len(exposed) == 1
    assert exposed[0].details["surface"] == "queue"
    assert len(reviewed_events) == 1
    assert reviewed_events[0].details["exposed"] is True
    assert reviewed_events[0].details["rejection_reason"] == "wrong_target"
    assert reviewed_events[0].details["review_kind"] == "individual"
    assert reviewed_events[0].details["review_duration_ms"] >= 0


def test_generation_ranking_snapshot_cannot_be_rewritten(db, site):
    source = _article(db, site, "source-immutable")
    target = _article(db, site, "target-immutable")
    suggestion = _suggestion(
        db,
        site,
        source,
        target,
        score_components={"version": "hybrid_bm25_v1", "bm25_score": 12.5},
        retrieval_version="hybrid_bm25_v1",
        ranking_version="hybrid_bm25:graph=shadow:feedback=off",
        final_rank=1,
        feature_snapshot={"bm25_score": 12.5},
    )

    with pytest.raises(IntegrityError, match="ranking snapshot is immutable"):
        db.execute(
            update(Suggestion)
            .where(Suggestion.id == suggestion.id)
            .values(feature_snapshot={"bm25_score": 1.0})
        )
        db.commit()
    db.rollback()

    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert stored is not None
    assert stored.feature_snapshot == {"bm25_score": 12.5}
    assert stored.retrieval_version == "hybrid_bm25_v1"


def test_rejection_reason_is_optional_but_status_scoped(client, db, site):
    source = _article(db, site, "source-reason")
    target = _article(db, site, "target-reason")
    suggestion = _suggestion(db, site, source, target)
    # Keep the review-event duration safe for historical rows as well as fresh
    # suggestions; a Python/PostgreSQL integer would overflow after ~24 days.
    suggestion.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    db.commit()

    invalid = client.put(
        f"/api/v1/suggestions/{suggestion.id}",
        json={"status": "approved", "rejection_reason": "other"},
    )
    assert invalid.status_code == 422

    valid = client.put(
        f"/api/v1/suggestions/{suggestion.id}",
        json={"status": "rejected"},
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["rejection_reason"] is None


def test_evaluation_distinguishes_exposed_and_unseen_decisions(client, db, site):
    source = _article(db, site, "source-metrics")
    target = _article(db, site, "target-metrics")
    target_two = _article(db, site, "target-metrics-two")
    exposed = _suggestion(db, site, source, target)
    unseen = _suggestion(db, site, source, target_two)

    response = client.post(
        "/api/v1/suggestions/exposure",
        json={"suggestion_ids": [exposed.id]},
    )
    assert response.status_code == 200, response.text
    assert client.put(
        f"/api/v1/suggestions/{exposed.id}",
        json={"status": "rejected", "rejection_reason": "not_relevant"},
    ).status_code == 200
    assert client.put(
        f"/api/v1/suggestions/{unseen.id}",
        json={"status": "rejected", "rejection_reason": "duplicate"},
    ).status_code == 200

    metrics = client.get("/api/v1/evaluation/metrics", params={"site_id": site.id})
    assert metrics.status_code == 200, metrics.text
    body = metrics.json()
    assert body["exposure"]["suggestions"] == 2
    assert body["exposure"]["exposed"] == 1
    assert body["exposure"]["unseen"] == 1
    assert body["exposure"]["exposed_decisions"] == 1
    assert body["exposure"]["unseen_decisions"] == 1
    assert body["exposure"]["exposed_acceptance_rate"] == 0.0
    assert body["rejection_reasons"] == [
        {"reason": "duplicate", "count": 1},
        {"reason": "not_relevant", "count": 1},
    ]
