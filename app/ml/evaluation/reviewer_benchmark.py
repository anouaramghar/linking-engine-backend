"""Ranking metrics over a frozen exposed reviewer-label dataset.

This module is the comparison seam for Slice 5B. It accepts precomputed ranked
target IDs and immutable reviewer labels, then returns relevance and negative-
judgment metrics. It has no database or production-ranking side effects, and it
does not decide whether a variant may be promoted.

Unknown targets are not treated as rejected: only explicitly judged targets are
counted in label precision and rejection-rate metrics. This keeps the benchmark
from turning an editor's unreviewed candidate pool into invented negatives.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.ml.evaluation.metrics import evaluate_rankings
from app.ml.evaluation.reviewer_labels import ReviewerLabelExample

SCHEMA_VERSION = "reviewer_benchmark_v1"


def _validate_rankings(rankings: Mapping[int, Sequence[int]]) -> None:
    for source_id, ranked in rankings.items():
        if len(set(ranked)) != len(ranked):
            raise ValueError(f"ranking for source {source_id} contains a duplicate target")


def _judgments(
    labels: Sequence[ReviewerLabelExample],
) -> tuple[dict[int, dict[int, ReviewerLabelExample]], int]:
    """Collapse repeated decisions for one source/target pair to the latest one."""
    by_pair: dict[tuple[int, int], ReviewerLabelExample] = {}
    duplicates = 0
    for label in labels:
        key = (label.source_article_id, label.target_article_id)
        previous = by_pair.get(key)
        if previous is not None:
            duplicates += 1
        if previous is None or (label.reviewed_at, label.review_event_id) > (
            previous.reviewed_at,
            previous.review_event_id,
        ):
            by_pair[key] = label

    grouped: dict[int, dict[int, ReviewerLabelExample]] = defaultdict(dict)
    for (source_id, target_id), label in by_pair.items():
        grouped[source_id][target_id] = label
    return dict(grouped), duplicates


@dataclass(frozen=True)
class ReviewerBenchmarkSummary:
    schema_version: str
    variant: str
    k: int
    queries: int
    scored_queries: int
    queries_with_approved_labels: int
    queries_rejected_only: int
    judged_labels: int
    approved_labels: int
    rejected_labels: int
    duplicate_pair_decisions: int
    judged_labels_found: int
    judged_labels_at_k: int
    approved_labels_at_k: int
    rejected_labels_at_k: int
    label_coverage: float
    label_coverage_at_k: float
    label_precision_at_k: float | None
    rejected_rate_at_k: float | None
    approved_hit_rate_at_k: float | None
    approved_recall_at_k: float | None
    ndcg_at_k: float | None
    mrr: float | None
    recall_ci95: tuple[float, float] | None
    ndcg_ci95: tuple[float, float] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "variant": self.variant,
            "k": self.k,
            "queries": self.queries,
            "scored_queries": self.scored_queries,
            "queries_with_approved_labels": self.queries_with_approved_labels,
            "queries_rejected_only": self.queries_rejected_only,
            "judged_labels": self.judged_labels,
            "approved_labels": self.approved_labels,
            "rejected_labels": self.rejected_labels,
            "duplicate_pair_decisions": self.duplicate_pair_decisions,
            "judged_labels_found": self.judged_labels_found,
            "judged_labels_at_k": self.judged_labels_at_k,
            "approved_labels_at_k": self.approved_labels_at_k,
            "rejected_labels_at_k": self.rejected_labels_at_k,
            "label_coverage": self.label_coverage,
            "label_coverage_at_k": self.label_coverage_at_k,
            "label_precision_at_k": self.label_precision_at_k,
            "rejected_rate_at_k": self.rejected_rate_at_k,
            "approved_hit_rate_at_k": self.approved_hit_rate_at_k,
            "approved_recall_at_k": self.approved_recall_at_k,
            "ndcg_at_k": self.ndcg_at_k,
            "mrr": self.mrr,
            "recall_ci95": list(self.recall_ci95) if self.recall_ci95 else None,
            "ndcg_ci95": list(self.ndcg_ci95) if self.ndcg_ci95 else None,
        }


@dataclass(frozen=True)
class ReviewerBenchmarkComparison:
    schema_version: str
    baseline_variant: str
    candidate_variant: str
    k: int
    queries: int
    reordered_queries: int
    approved_hit_gain_queries: int
    approved_hit_loss_queries: int
    approved_hit_unchanged_queries: int
    rejected_hit_gain_queries: int
    rejected_hit_loss_queries: int
    rejected_hit_unchanged_queries: int
    ndcg_delta: float | None
    mrr_delta: float | None
    label_precision_delta: float | None
    rejected_rate_delta: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "baseline_variant": self.baseline_variant,
            "candidate_variant": self.candidate_variant,
            "k": self.k,
            "queries": self.queries,
            "reordered_queries": self.reordered_queries,
            "approved_hit_gain_queries": self.approved_hit_gain_queries,
            "approved_hit_loss_queries": self.approved_hit_loss_queries,
            "approved_hit_unchanged_queries": self.approved_hit_unchanged_queries,
            "rejected_hit_gain_queries": self.rejected_hit_gain_queries,
            "rejected_hit_loss_queries": self.rejected_hit_loss_queries,
            "rejected_hit_unchanged_queries": self.rejected_hit_unchanged_queries,
            "ndcg_delta": self.ndcg_delta,
            "mrr_delta": self.mrr_delta,
            "label_precision_delta": self.label_precision_delta,
            "rejected_rate_delta": self.rejected_rate_delta,
        }


def evaluate_reviewer_rankings(
    rankings: Mapping[int, Sequence[int]],
    labels: Sequence[ReviewerLabelExample],
    *,
    variant: str,
    k: int,
) -> ReviewerBenchmarkSummary:
    """Score one ordering against explicit approved and rejected judgments."""
    if k < 1:
        raise ValueError("k must be at least 1")
    _validate_rankings(rankings)
    judgments, duplicate_pairs = _judgments(labels)
    relevant_by_source = {
        source_id: {
            target_id
            for target_id, label in targets.items()
            if label.label == "approved"
        }
        for source_id, targets in judgments.items()
    }
    positive_queries = sum(bool(relevant) for relevant in relevant_by_source.values())
    rejected_only_queries = len(judgments) - positive_queries
    metric_summary = evaluate_rankings(rankings, relevant_by_source, k=k)

    judged_labels = approved_labels = rejected_labels = 0
    judged_labels_found = judged_labels_at_k = 0
    approved_labels_at_k = rejected_labels_at_k = 0
    approved_hit_queries = 0
    for source_id, target_labels in judgments.items():
        judged_labels += len(target_labels)
        approved_labels += sum(label.label == "approved" for label in target_labels.values())
        rejected_labels += sum(label.label == "rejected" for label in target_labels.values())
        ranked = rankings.get(source_id, ())
        ranked_judged = [target_id for target_id in ranked if target_id in target_labels]
        top_judged = [target_id for target_id in ranked[:k] if target_id in target_labels]
        judged_labels_found += len(ranked_judged)
        judged_labels_at_k += len(top_judged)
        approved_labels_at_k += sum(
            target_labels[target_id].label == "approved" for target_id in top_judged
        )
        rejected_labels_at_k += sum(
            target_labels[target_id].label == "rejected" for target_id in top_judged
        )
        if any(target_labels[target_id].label == "approved" for target_id in top_judged):
            approved_hit_queries += 1

    return ReviewerBenchmarkSummary(
        schema_version=SCHEMA_VERSION,
        variant=variant,
        k=k,
        queries=len(judgments),
        scored_queries=metric_summary.queries,
        queries_with_approved_labels=positive_queries,
        queries_rejected_only=rejected_only_queries,
        judged_labels=judged_labels,
        approved_labels=approved_labels,
        rejected_labels=rejected_labels,
        duplicate_pair_decisions=duplicate_pairs,
        judged_labels_found=judged_labels_found,
        judged_labels_at_k=judged_labels_at_k,
        approved_labels_at_k=approved_labels_at_k,
        rejected_labels_at_k=rejected_labels_at_k,
        label_coverage=judged_labels_found / judged_labels if judged_labels else 0.0,
        label_coverage_at_k=judged_labels_at_k / judged_labels if judged_labels else 0.0,
        label_precision_at_k=(
            approved_labels_at_k / judged_labels_at_k if judged_labels_at_k else None
        ),
        rejected_rate_at_k=(
            rejected_labels_at_k / judged_labels_at_k if judged_labels_at_k else None
        ),
        approved_hit_rate_at_k=(
            approved_hit_queries / positive_queries if positive_queries else None
        ),
        approved_recall_at_k=(metric_summary.recall_at_k if positive_queries else None),
        ndcg_at_k=(metric_summary.ndcg_at_k if positive_queries else None),
        mrr=(metric_summary.mrr if positive_queries else None),
        recall_ci95=(metric_summary.recall_ci95 if positive_queries else None),
        ndcg_ci95=(metric_summary.ndcg_ci95 if positive_queries else None),
    )


def compare_reviewer_rankings(
    candidate_rankings: Mapping[int, Sequence[int]],
    baseline_rankings: Mapping[int, Sequence[int]],
    labels: Sequence[ReviewerLabelExample],
    *,
    baseline_variant: str,
    candidate_variant: str,
    k: int,
) -> ReviewerBenchmarkComparison:
    """Report paired changes without collapsing positive and negative judgments."""
    candidate_summary = evaluate_reviewer_rankings(
        candidate_rankings,
        labels,
        variant=candidate_variant,
        k=k,
    )
    baseline_summary = evaluate_reviewer_rankings(
        baseline_rankings,
        labels,
        variant=baseline_variant,
        k=k,
    )
    judgments, _duplicates = _judgments(labels)
    reordered = 0
    approved_gain = approved_loss = approved_unchanged = 0
    rejected_gain = rejected_loss = rejected_unchanged = 0
    for source_id, target_labels in judgments.items():
        baseline = baseline_rankings.get(source_id, ())
        candidate = candidate_rankings.get(source_id, ())
        if list(candidate) != list(baseline):
            reordered += 1
        approved = {
            target_id for target_id, label in target_labels.items() if label.label == "approved"
        }
        if approved:
            baseline_hits = sum(target_id in approved for target_id in baseline[:k])
            candidate_hits = sum(target_id in approved for target_id in candidate[:k])
            if candidate_hits > baseline_hits:
                approved_gain += 1
            elif candidate_hits < baseline_hits:
                approved_loss += 1
            else:
                approved_unchanged += 1
        rejected = {
            target_id for target_id, label in target_labels.items() if label.label == "rejected"
        }
        if rejected:
            baseline_hits = sum(target_id in rejected for target_id in baseline[:k])
            candidate_hits = sum(target_id in rejected for target_id in candidate[:k])
            if candidate_hits > baseline_hits:
                rejected_gain += 1
            elif candidate_hits < baseline_hits:
                rejected_loss += 1
            else:
                rejected_unchanged += 1

    return ReviewerBenchmarkComparison(
        schema_version=SCHEMA_VERSION,
        baseline_variant=baseline_variant,
        candidate_variant=candidate_variant,
        k=k,
        queries=len(judgments),
        reordered_queries=reordered,
        approved_hit_gain_queries=approved_gain,
        approved_hit_loss_queries=approved_loss,
        approved_hit_unchanged_queries=approved_unchanged,
        rejected_hit_gain_queries=rejected_gain,
        rejected_hit_loss_queries=rejected_loss,
        rejected_hit_unchanged_queries=rejected_unchanged,
        ndcg_delta=_delta(candidate_summary.ndcg_at_k, baseline_summary.ndcg_at_k),
        mrr_delta=_delta(candidate_summary.mrr, baseline_summary.mrr),
        label_precision_delta=_delta(
            candidate_summary.label_precision_at_k,
            baseline_summary.label_precision_at_k,
        ),
        rejected_rate_delta=_delta(
            candidate_summary.rejected_rate_at_k,
            baseline_summary.rejected_rate_at_k,
        ),
    )


def _delta(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return candidate - baseline
