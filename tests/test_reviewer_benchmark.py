from datetime import UTC, datetime, timedelta

import pytest

from app.ml.evaluation.reviewer_benchmark import (
    ReviewerBenchmarkSummary,
    compare_reviewer_rankings,
    evaluate_reviewer_rankings,
)
from app.ml.evaluation.reviewer_labels import ReviewerLabelExample


def _label(*, source_id: int, target_id: int, label: str, event_id: int) -> ReviewerLabelExample:
    reviewed_at = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=event_id)
    return ReviewerLabelExample(
        review_event_id=event_id,
        suggestion_id=event_id,
        trace_id=f"trace-{event_id}",
        site_id=1,
        source_article_id=source_id,
        target_article_id=target_id,
        label=label,
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


LABELS = (
    _label(source_id=10, target_id=1, label="approved", event_id=1),
    _label(source_id=10, target_id=2, label="rejected", event_id=2),
    _label(source_id=20, target_id=3, label="rejected", event_id=3),
)


def test_reviewer_metrics_score_positive_and_negative_judgments():
    baseline = evaluate_reviewer_rankings(
        {10: [2, 1, 99], 20: [88]},
        LABELS,
        variant="bm25_512",
        k=1,
    )
    candidate = evaluate_reviewer_rankings(
        {10: [1, 2, 99], 20: [88]},
        LABELS,
        variant="active",
        k=1,
    )

    assert isinstance(baseline, ReviewerBenchmarkSummary)
    assert baseline.queries == 2
    assert baseline.queries_with_approved_labels == 1
    assert baseline.queries_rejected_only == 1
    assert baseline.approved_labels == 1
    assert baseline.rejected_labels == 2
    assert baseline.judged_labels_at_k == 1
    assert baseline.approved_labels_at_k == 0
    assert baseline.rejected_labels_at_k == 1
    assert baseline.label_precision_at_k == 0.0
    assert baseline.rejected_rate_at_k == 1.0

    assert candidate.approved_labels_at_k == 1
    assert candidate.rejected_labels_at_k == 0
    assert candidate.label_precision_at_k == 1.0
    assert candidate.rejected_rate_at_k == 0.0
    assert candidate.approved_hit_rate_at_k == 1.0
    assert candidate.ndcg_at_k == 1.0


def test_comparison_reports_paired_relevance_and_rejection_changes():
    comparison = compare_reviewer_rankings(
        {10: [1, 2], 20: [88]},
        {10: [2, 1], 20: [88]},
        LABELS,
        baseline_variant="bm25_512",
        candidate_variant="active",
        k=1,
    )

    assert comparison.queries == 2
    assert comparison.reordered_queries == 1
    assert comparison.approved_hit_gain_queries == 1
    assert comparison.approved_hit_loss_queries == 0
    assert comparison.rejected_hit_gain_queries == 0
    assert comparison.rejected_hit_loss_queries == 1
    assert comparison.ndcg_delta > 0
    assert comparison.label_precision_delta == 1.0


def test_reviewer_metric_rejects_duplicate_ranked_targets():
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_reviewer_rankings(
            {10: [1, 1]},
            LABELS,
            variant="bm25_512",
            k=1,
        )
