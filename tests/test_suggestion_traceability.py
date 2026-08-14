from uuid import uuid4

from sqlalchemy import select

from app.models import Article, Suggestion, SuggestionEvent


def _suggestion(db, site) -> Suggestion:
    suffix = uuid4().hex[:8]
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source-{suffix}",
        title="Traceable source",
        content_text="Source content",
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/target-{suffix}",
        title="Traceable target",
        content_text="Target content",
    )
    db.add_all([source, target])
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=0.82,
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def test_generated_suggestion_gets_trace_id_and_initial_event(client, db, site):
    suggestion = _suggestion(db, site)

    queue = client.get(f"/api/v1/suggestions/{site.id}")
    assert queue.status_code == 200
    row = queue.json()[0]
    assert row["trace_id"] == suggestion.trace_id
    assert len(row["trace_id"]) == 36

    history = client.get(f"/api/v1/suggestions/{suggestion.id}/events")
    assert history.status_code == 200
    assert history.json() == [
        {
            "id": history.json()[0]["id"],
            "suggestion_id": suggestion.id,
            "event_type": "generated",
            "actor": "analysis-engine",
            "details": {
                "method": "baseline_cosine",
                "score": 0.82,
                "status": "pending",
            },
            "created_at": history.json()[0]["created_at"],
        }
    ]


def test_review_and_undo_append_actor_attributed_events(client, db, site):
    suggestion = _suggestion(db, site)

    approved = client.put(
        f"/api/v1/suggestions/{suggestion.id}",
        json={"status": "approved"},
    )
    assert approved.status_code == 200
    restored = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [suggestion.id], "status": "pending"},
    )
    assert restored.status_code == 200

    history = client.get(f"/api/v1/suggestions/{suggestion.id}/events").json()
    assert [event["event_type"] for event in history] == [
        "restored",
        "reviewed",
        "generated",
    ]
    assert history[0]["actor"] == "local-development"
    assert history[0]["details"] == {
        "from_status": "approved",
        "to_status": "pending",
    }
    assert history[1]["actor"] == "local-development"
    assert history[1]["details"]["from_status"] == "pending"
    assert history[1]["details"]["to_status"] == "approved"
    assert history[1]["details"]["reviewer_id"] == "local-development"
    assert history[1]["details"]["review_kind"] == "individual"
    assert history[1]["details"]["exposed"] is False
    assert history[1]["details"]["review_duration_ms"] >= 0


def test_worker_owned_transition_is_attributed_and_explained(client, db, site):
    suggestion = _suggestion(db, site)
    suggestion.status = "applied"
    suggestion.publish_outcome = "inserted"
    db.commit()

    latest = client.get(f"/api/v1/suggestions/{suggestion.id}/events").json()[0]
    assert latest["event_type"] == "applied"
    assert latest["actor"] == "publication-worker"
    assert latest["details"] == {
        "from_status": "pending",
        "to_status": "applied",
        "publish_outcome": "inserted",
    }


def test_suggestion_event_history_is_immutable_and_scoped(client, db, site):
    first = _suggestion(db, site)
    second = _suggestion(db, site)

    first_events = client.get(f"/api/v1/suggestions/{first.id}/events").json()
    assert {event["suggestion_id"] for event in first_events} == {first.id}
    assert (
        db.scalar(select(SuggestionEvent).where(SuggestionEvent.suggestion_id == second.id))
        is not None
    )

    missing = client.get("/api/v1/suggestions/999999999/events")
    assert missing.status_code == 404
