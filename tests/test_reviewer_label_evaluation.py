from datetime import UTC, datetime, timedelta

import pytest

from app.ml.evaluation.reviewer_labels import (
    LabelReadinessError,
    ReviewerLabelDataset,
    ReviewerLabelExample,
    build_reviewer_label_dataset,
    build_site_holdout_split,
    build_time_split,
    eligible_reviewer_labels,
    inspect_label_readiness,
)
from app.ml.evaluation.reviewer_benchmark_runner import run_reviewer_benchmark
from app.models import Article, Suggestion, SuggestionEvent


CUTOFF = datetime(2026, 8, 10, tzinfo=UTC)


def _article(db, site, slug: str) -> Article:
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/{slug}",
        title=slug,
        content_text=f"content for {slug}",
        is_active=True,
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db.add(article)
    db.flush()
    return article


def _label(
    db,
    site,
    source: Article,
    target: Article,
    *,
    reviewed_at: datetime,
    label: str = "approved",
    review_kind: str = "individual",
    exposed: bool = True,
    reviewer_id: str | None = "reviewer-1",
    complete_snapshot: bool = True,
) -> Suggestion:
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        status=label,
        created_at=reviewed_at - timedelta(hours=1),
        reviewed_at=reviewed_at,
        shown_at=reviewed_at - timedelta(minutes=5) if exposed else None,
        exposure_count=1 if exposed else 0,
        reviewer_id=reviewer_id,
        retrieval_version="hybrid_bm25_v1" if complete_snapshot else None,
        ranking_version="hybrid_bm25:graph=shadow:feedback=off" if complete_snapshot else None,
        final_rank=1 if complete_snapshot else None,
        feature_snapshot={"bm25_score": 12.5} if complete_snapshot else None,
    )
    db.add(suggestion)
    db.flush()
    db.add(
        SuggestionEvent(
            suggestion_id=suggestion.id,
            event_type="reviewed",
            actor=reviewer_id or "unknown-reviewer",
            created_at=reviewed_at,
            details={
                "from_status": "pending",
                "to_status": label,
                "review_kind": review_kind,
                "reviewer_id": reviewer_id,
                "exposed": exposed,
            },
        )
    )
    db.commit()
    db.refresh(suggestion)
    return suggestion


def test_label_readiness_and_export_exclude_unseen_bulk_and_incomplete_rows(db, site):
    source = _article(db, site, "source")
    target = _article(db, site, "target")
    positive = _label(db, site, source, target, reviewed_at=datetime(2026, 8, 5, tzinfo=UTC))
    _label(
        db,
        site,
        source,
        _article(db, site, "unseen-target"),
        reviewed_at=datetime(2026, 8, 6, tzinfo=UTC),
        exposed=False,
    )
    _label(
        db,
        site,
        source,
        _article(db, site, "bulk-target"),
        reviewed_at=datetime(2026, 8, 7, tzinfo=UTC),
        review_kind="bulk",
    )
    _label(
        db,
        site,
        source,
        _article(db, site, "missing-snapshot-target"),
        reviewed_at=datetime(2026, 8, 8, tzinfo=UTC),
        complete_snapshot=False,
    )
    _label(
        db,
        site,
        source,
        _article(db, site, "missing-reviewer-target"),
        reviewed_at=datetime(2026, 8, 9, tzinfo=UTC),
        reviewer_id=None,
    )

    readiness = inspect_label_readiness(db, site_ids=(site.id,))
    assert readiness.individual_labels == 4
    assert readiness.bulk_labels == 1
    assert readiness.exposed_individual_labels == 3
    assert readiness.eligible_labels == 1
    assert readiness.site_counts[0].eligible_labels == 1
    assert readiness.ready is False

    labels = eligible_reviewer_labels(db, site_ids=(site.id,))
    assert [row.suggestion_id for row in labels] == [positive.id]
    assert labels[0].label == "approved"
    assert labels[0].reviewer_id == "reviewer-1"


def test_admin_exports_only_eligible_labels_with_frozen_split_metadata(client, db, site):
    source = _article(db, site, "api-source")
    positive = _label(
        db,
        site,
        source,
        _article(db, site, "api-target"),
        reviewed_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    _label(
        db,
        site,
        source,
        _article(db, site, "api-unseen"),
        reviewed_at=datetime(2026, 8, 6, tzinfo=UTC),
        exposed=False,
    )

    response = client.get(
        "/api/v1/evaluation/reviewer-labels.json",
        params={"site_id": site.id, "cutoff_at": CUTOFF.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["readiness"]["ready"] is False
    assert [row["suggestion_id"] for row in body["labels"]] == [positive.id]
    assert [row["suggestion_id"] for row in body["time_split"]["train"]] == [positive.id]
    assert body["time_split"]["test"] == []
    dataset = build_reviewer_label_dataset(
        db,
        cutoff_at=CUTOFF,
        site_ids=(site.id,),
        require_ready=False,
    )
    assert ReviewerLabelDataset.from_dict(dataset.to_dict()) == dataset

    csv_response = client.get(
        "/api/v1/evaluation/reviewer-labels.csv",
        params={"site_id": site.id, "cutoff_at": CUTOFF.isoformat()},
    )
    assert csv_response.status_code == 200, csv_response.text
    assert "linkmesh-reviewer-labels.csv" in csv_response.headers["content-disposition"]
    assert f",{positive.id}," in csv_response.text
    assert "api-unseen" not in csv_response.text


def test_frozen_dataset_gate_is_fail_closed_until_three_sites_are_ready(db, site):
    source = _article(db, site, "source-gate")
    target = _article(db, site, "target-gate")
    _label(db, site, source, target, reviewed_at=datetime(2026, 8, 5, tzinfo=UTC))

    with pytest.raises(LabelReadinessError) as error:
        build_reviewer_label_dataset(
            db,
            cutoff_at=CUTOFF,
            site_ids=(site.id,),
        )

    assert error.value.readiness.ready is False
    assert "three representative sites" in " ".join(error.value.readiness.blocked_reasons)


def test_benchmark_runner_refuses_an_unready_evidence_export(db, site):
    source = _article(db, site, "source-runner-gate")
    target = _article(db, site, "target-runner-gate")
    _label(db, site, source, target, reviewed_at=datetime(2026, 8, 5, tzinfo=UTC))
    dataset = build_reviewer_label_dataset(
        db,
        cutoff_at=CUTOFF,
        site_ids=(site.id,),
        require_ready=False,
    )

    with pytest.raises(LabelReadinessError):
        run_reviewer_benchmark(db, dataset)


def _example(*, site_id: int, suggestion_id: int, reviewed_at: datetime) -> ReviewerLabelExample:
    return ReviewerLabelExample(
        review_event_id=suggestion_id + 1000,
        suggestion_id=suggestion_id,
        trace_id=f"trace-{suggestion_id}",
        site_id=site_id,
        source_article_id=site_id * 10,
        target_article_id=site_id * 10 + 1,
        label="approved",
        reviewed_at=reviewed_at,
        reviewer_id="reviewer-1",
        shown_at=reviewed_at - timedelta(minutes=1),
        exposure_count=1,
        method="hybrid_bm25",
        score=0.8,
        retrieval_version="hybrid_bm25_v1",
        ranking_version="hybrid_bm25:graph=shadow:feedback=off",
        final_rank=1,
        feature_snapshot={"bm25_score": 12.5},
    )


def test_time_split_is_frozen_and_site_holdout_has_no_site_overlap():
    labels = (
        _example(site_id=1, suggestion_id=1, reviewed_at=datetime(2026, 8, 1, tzinfo=UTC)),
        _example(site_id=2, suggestion_id=2, reviewed_at=datetime(2026, 8, 10, tzinfo=UTC)),
        _example(site_id=3, suggestion_id=3, reviewed_at=datetime(2026, 8, 11, tzinfo=UTC)),
    )

    time_split = build_time_split(labels, cutoff_at=CUTOFF)
    assert [row.suggestion_id for row in time_split.train] == [1]
    assert [row.suggestion_id for row in time_split.test] == [2, 3]
    assert time_split.train_site_ids == (1,)
    assert time_split.test_site_ids == (2, 3)
    assert time_split.site_overlap == ()

    site_split = build_site_holdout_split(labels, holdout_site_id=2)
    assert [row.suggestion_id for row in site_split.train] == [1, 3]
    assert [row.suggestion_id for row in site_split.test] == [2]
    assert site_split.train_site_ids == (1, 3)
    assert site_split.test_site_ids == (2,)
    assert site_split.site_overlap == ()
