from uuid import uuid4

from app.models import Article, Suggestion


def _suggestion(db, site) -> Suggestion:
    suffix = uuid4().hex[:8]
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/trace-source-{suffix}",
        title="Trace dashboard source",
        content_text="Source",
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/trace-target-{suffix}",
        title="Trace dashboard target",
        content_text="Target",
    )
    db.add_all([source, target])
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.87,
        rank_score=0.87,
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def test_trace_dashboard_filters_full_details_and_csv(client, db, site):
    suggestion = _suggestion(db, site)
    suggestion.status = "failed"
    suggestion.publish_error = "WordPress returned 503"
    db.commit()

    response = client.get(
        "/api/v1/suggestion-events",
        params={
            "trace_id": suggestion.trace_id,
            "actor": "publication-worker",
            "event_type": "failed",
            "status": "failed",
            "site_id": site.id,
        },
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    event = page["items"][0]
    assert event["trace_id"] == suggestion.trace_id
    assert event["source_title"] == "Trace dashboard source"
    assert event["target_title"] == "Trace dashboard target"
    assert event["publish_error"] == "WordPress returned 503"
    assert event["details"]["publish_error"] == "WordPress returned 503"

    exported = client.get(
        "/api/v1/suggestion-events/export.csv",
        params={"trace_id": suggestion.trace_id},
    )
    assert exported.status_code == 200
    assert "linkmesh-traceability.csv" in exported.headers["content-disposition"]
    assert suggestion.trace_id in exported.text
    assert "WordPress returned 503" in exported.text


def test_trace_dashboard_rejects_an_inverted_date_range(client):
    response = client.get(
        "/api/v1/suggestion-events",
        params={
            "date_from": "2026-08-10T12:00:00Z",
            "date_to": "2026-08-09T12:00:00Z",
        },
    )
    assert response.status_code == 422
