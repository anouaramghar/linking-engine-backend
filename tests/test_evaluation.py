from datetime import UTC, date, datetime, timedelta

import pytest

from app.models import (
    Article,
    EvaluationSnapshot,
    InternalLink,
    Suggestion,
    SuggestionEvent,
)
from app.services.evaluation_service import capture_daily_evaluation_snapshots


def _article(db, site, slug: str) -> Article:
    article = Article(
        site_id=site.id,
        external_id=slug,
        url=f"{site.base_url}/{slug}",
        title=slug,
        content_text=f"content for {slug}",
    )
    db.add(article)
    db.flush()
    return article


def test_evaluation_metrics_use_real_editorial_and_delivery_data(client, db, site):
    base = datetime(2026, 8, 1, 8, tzinfo=UTC)
    source = _article(db, site, "source")
    linked = _article(db, site, "linked")
    helped_orphan = _article(db, site, "helped-orphan")
    rejected_target = _article(db, site, "rejected")
    pending_target = _article(db, site, "pending")
    db.add(
        InternalLink(
            source_article_id=source.id,
            target_article_id=linked.id,
            first_seen_at=base - timedelta(days=5),
        )
    )
    db.add_all(
        [
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=linked.id,
                method="hybrid_bm25",
                score=0.91,
                rank_score=0.91,
                status="approved",
                created_at=base,
                reviewed_at=base + timedelta(hours=2),
                placement_generated_at=base + timedelta(hours=1),
                placement_context="A useful passage about linked content.",
                anchor_text="linked content",
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=rejected_target.id,
                method="hybrid_bm25",
                score=0.72,
                rank_score=0.72,
                status="rejected",
                created_at=base,
                reviewed_at=base + timedelta(hours=4),
                placement_generated_at=base + timedelta(hours=1),
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=helped_orphan.id,
                method="baseline_cosine",
                score=0.84,
                rank_score=0.84,
                status="applied",
                created_at=base,
                reviewed_at=base + timedelta(hours=6),
                applied_at=base + timedelta(hours=7),
                publish_outcome="inserted",
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=linked.id,
                target_article_id=rejected_target.id,
                method="baseline_cosine",
                score=0.63,
                rank_score=0.63,
                status="failed",
                created_at=base,
                reviewed_at=base + timedelta(hours=8),
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=pending_target.id,
                method="baseline_cosine",
                score=0.8,
                rank_score=0.8,
                status="pending",
                created_at=base,
            ),
        ]
    )
    db.commit()

    response = client.get("/api/v1/evaluation/metrics", params={"site_id": site.id})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["site_id"] == site.id
    assert payload["date_from"] is None
    assert payload["date_to"] is None
    assert "suggestions generated" in payload["cohort_definition"]
    assert payload["comparison"] is None
    assert payload["trend"]
    assert payload["orphan_trend"] == []
    assert payload["editorial"] == {
        "suggestions_total": 5,
        "pending": 1,
        "accepted": 3,
        "rejected": 1,
        "decisions": 4,
        "acceptance_rate": 0.75,
        "rejection_rate": 0.25,
        "average_decision_hours": 5.0,
        "median_decision_hours": 5.0,
        "decision_time_sample": 4,
    }
    assert payload["placement"] == {
        "generated": 2,
        "successful": 1,
        "success_rate": 0.5,
    }
    assert payload["publication"] == {
        "completed": 2,
        "succeeded": 1,
        "failed": 1,
        "success_rate": 0.5,
        "failure_rate": 0.5,
    }
    assert payload["orphans"] == {
        "active_articles": 5,
        "remaining": 4,
        "reduced_by_linkmesh": 1,
    }
    assert payload["methods"] == [
        {
            "method": "baseline_cosine",
            "suggestions": 3,
            "pending": 1,
            "accepted": 2,
            "rejected": 0,
            "applied": 1,
            "acceptance_rate": 1.0,
            "average_semantic_score": pytest.approx(0.7567),
        },
        {
            "method": "hybrid_bm25",
            "suggestions": 2,
            "pending": 0,
            "accepted": 1,
            "rejected": 1,
            "applied": 0,
            "acceptance_rate": 0.5,
            "average_semantic_score": 0.815,
        },
    ]
    ranges = {item["label"]: item for item in payload["score_ranges"]}
    assert ranges["60-69%"]["acceptance_rate"] == 1.0
    assert ranges["70-79%"]["acceptance_rate"] == 0.0
    assert ranges["80-89%"] == {
        "label": "80-89%",
        "minimum": 80,
        "maximum": 89,
        "suggestions": 2,
        "pending": 1,
        "accepted": 1,
        "rejected": 0,
        "acceptance_rate": 1.0,
    }
    assert payload["sites"] == [
        {
            "site_id": site.id,
            "site_name": site.name,
            "suggestions": 5,
            "pending": 1,
            "accepted": 3,
            "rejected": 1,
            "applied": 1,
            "acceptance_rate": 0.75,
        }
    ]


def test_evaluation_metrics_return_null_rates_without_samples(client, site):
    response = client.get("/api/v1/evaluation/metrics", params={"site_id": site.id})

    assert response.status_code == 200
    payload = response.json()
    assert payload["editorial"]["acceptance_rate"] is None
    assert payload["editorial"]["median_decision_hours"] is None
    assert payload["placement"]["success_rate"] is None
    assert payload["publication"]["success_rate"] is None
    assert payload["methods"] == []
    assert payload["sites"][0]["suggestions"] == 0


def test_evaluation_metrics_reject_unknown_site(client):
    response = client.get("/api/v1/evaluation/metrics", params={"site_id": 2_147_483_647})

    assert response.status_code == 404


def test_date_filter_returns_one_cohort_with_previous_period_comparison(client, db, site):
    source = _article(db, site, "filter-source")
    target = _article(db, site, "filter-target")
    db.add_all(
        [
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=target.id,
                method="hybrid_bm25",
                score=0.9,
                rank_score=0.9,
                status="approved",
                created_at=datetime(2026, 8, 3, tzinfo=UTC),
                reviewed_at=datetime(2026, 8, 4, tzinfo=UTC),
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=target.id,
                target_article_id=source.id,
                method="baseline_cosine",
                score=0.7,
                rank_score=0.7,
                status="rejected",
                created_at=datetime(2026, 7, 29, tzinfo=UTC),
                reviewed_at=datetime(2026, 7, 30, tzinfo=UTC),
            ),
        ]
    )
    db.commit()

    response = client.get(
        "/api/v1/evaluation/metrics",
        params={
            "site_id": site.id,
            "date_from": "2026-08-01T00:00:00Z",
            "date_to": "2026-08-06T00:00:00Z",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["editorial"]["suggestions_total"] == 1
    assert payload["editorial"]["acceptance_rate"] == 1.0
    assert payload["methods"][0]["method"] == "hybrid_bm25"
    assert payload["comparison"]["suggestions_change_rate"] == 0.0
    assert payload["comparison"]["acceptance_rate_change"] == 1.0
    generated = [point for point in payload["trend"] if point["generated"]]
    assert generated == [
        {
            "bucket_start": "2026-08-03",
            "generated": 1,
            "accepted": 1,
            "rejected": 0,
            "applied": 0,
            "acceptance_rate": 1.0,
        }
    ]


def test_daily_orphan_snapshot_is_idempotent_and_visible_in_trend(client, db, site):
    source = _article(db, site, "snapshot-source")
    target = _article(db, site, "snapshot-target")
    db.commit()

    captured = capture_daily_evaluation_snapshots(db, date(2026, 8, 5))
    assert captured >= 1
    db.commit()
    first = client.get(
        "/api/v1/evaluation/metrics",
        params={"site_id": site.id},
    ).json()
    assert first["orphan_trend"] == [
        {
            "snapshot_date": "2026-08-05",
            "active_articles": 2,
            "remaining": 2,
        }
    ]

    db.add(InternalLink(source_article_id=source.id, target_article_id=target.id))
    db.commit()
    assert capture_daily_evaluation_snapshots(db, date(2026, 8, 5)) == captured
    db.commit()

    assert db.query(EvaluationSnapshot).filter_by(site_id=site.id).count() == 1
    refreshed = client.get(
        "/api/v1/evaluation/metrics",
        params={"site_id": site.id},
    ).json()
    assert refreshed["orphan_trend"][0]["remaining"] == 1


def test_drilldown_and_csv_export_respect_filters_and_escape_formulas(client, db, site):
    source = _article(db, site, "export-source")
    source.title = "=DANGEROUS()"
    target = _article(db, site, "export-target")
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.88,
        rank_score=0.88,
        status="approved",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        reviewed_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    db.add(suggestion)
    db.commit()

    params = {
        "site_id": site.id,
        "date_from": "2026-08-01T00:00:00Z",
        "date_to": "2026-08-06T00:00:00Z",
    }
    drilldown = client.get(
        "/api/v1/evaluation/suggestions",
        params={**params, "metric": "accepted"},
    )
    assert drilldown.status_code == 200, drilldown.text
    page = drilldown.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == suggestion.id
    assert page["items"][0]["source_title"] == "=DANGEROUS()"
    assert (
        client.get(
            "/api/v1/evaluation/suggestions",
            params={**params, "metric": "rejected"},
        ).json()["total"]
        == 0
    )

    exported = client.get("/api/v1/evaluation/export.csv", params=params)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "linkmesh-evaluation.csv" in exported.headers["content-disposition"]
    assert "'=DANGEROUS()" in exported.text
    assert suggestion.trace_id in exported.text


def test_date_filters_require_timezone_and_chronological_order(client, site):
    naive = client.get(
        "/api/v1/evaluation/metrics",
        params={"site_id": site.id, "date_from": "2026-08-01T00:00:00"},
    )
    backwards = client.get(
        "/api/v1/evaluation/metrics",
        params={
            "site_id": site.id,
            "date_from": "2026-08-06T00:00:00Z",
            "date_to": "2026-08-01T00:00:00Z",
        },
    )

    assert naive.status_code == 422
    assert backwards.status_code == 422


def test_failed_publication_drilldown_reports_the_failure_not_the_review(client, db, site):
    """`reviewed_at` is when an editor approved the row, which is before every
    attempt that could fail. Reporting it as the failure time puts the failure
    days ahead of its own cause.
    """
    source = _article(db, site, "failure-source")
    target = _article(db, site, "failure-target")
    approved_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
    failed_at = datetime(2026, 8, 5, 17, 30, tzinfo=UTC)
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        rank_score=0.8,
        status="failed",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        reviewed_at=approved_at,
    )
    db.add(suggestion)
    db.flush()
    db.add_all(
        [
            SuggestionEvent(
                suggestion_id=suggestion.id,
                event_type="publish_attempt_failed",
                actor="system:publication",
                details={"attempt": 1},
                created_at=failed_at - timedelta(hours=2),
            ),
            SuggestionEvent(
                suggestion_id=suggestion.id,
                event_type="failed",
                actor="publication-worker",
                details={},
                created_at=failed_at,
            ),
        ]
    )
    db.commit()

    page = client.get(
        "/api/v1/evaluation/suggestions",
        params={"site_id": site.id, "metric": "publish_failed"},
    ).json()

    assert page["total"] == 1
    assert page["items"][0]["occurred_at"].startswith("2026-08-05T17:30")

    exported = client.get("/api/v1/evaluation/export.csv", params={"site_id": site.id})
    assert "2026-08-05T17:30" in exported.text


def test_metrics_declare_what_the_dashboard_is_and_is_not(client, db, site):
    """Numbers without provenance get quoted as evidence. These say they are not."""
    source = _article(db, site, "provenance-source")
    target = _article(db, site, "provenance-target")
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=0.8,
            rank_score=0.8,
            status="approved",
            created_at=datetime(2026, 8, 2, tzinfo=UTC),
            reviewed_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
    )
    db.commit()

    provenance = client.get("/api/v1/evaluation/metrics", params={"site_id": site.id}).json()[
        "provenance"
    ]

    assert provenance["surface"] == "operational_telemetry"
    assert provenance["supports_ranking_decisions"] is False
    assert provenance["schema_version"]
    assert provenance["evidence_cutoff"] is not None
    assert provenance["sample_state"] == "more_individual_labels_required"
    assert provenance["individual_labels"] == 1
    assert provenance["bulk_labels"] == 0
    assert provenance["individual_label_target"] == 100
    assert provenance["baseline_site_target"] == 3
    assert provenance["sites_meeting_label_target"] == 0
    assert len(provenance["limitations"]) >= 3
