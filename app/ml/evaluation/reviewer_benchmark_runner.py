"""Run BM25-512 and deterministic graph variants on a frozen label split."""

from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.config import settings
from app.ml.evaluation.graph_metrics import build_as_of_graph
from app.ml.evaluation.reviewer_benchmark import (
    compare_reviewer_rankings,
    evaluate_reviewer_rankings,
)
from app.ml.evaluation.reviewer_labels import (
    LabelReadinessError,
    ReviewerLabelDataset,
    ReviewerLabelExample,
)
from app.ml.evaluation.ranking import EvaluationRanker
from app.services.graph_service import deterministic_rerank

BenchmarkSplit = Literal["time", "site_holdout"]

# `bm25_512` is the production final-order baseline. `off` and `shadow` are
# explicit controls: both must preserve the baseline order. `active` is measured
# as a comparison only and never changes production configuration.
VARIANTS = ("bm25_512", "off", "shadow", "active")
BASELINE_VARIANT = "bm25_512"


class ReviewerBenchmarkRunError(RuntimeError):
    """The frozen artifact cannot be measured against the current database."""


def run_reviewer_benchmark(
    db: Session,
    dataset: ReviewerLabelDataset,
    *,
    split_mode: BenchmarkSplit = "time",
    k: int = 5,
    variants: Sequence[str] = VARIANTS,
    site_ids: Collection[int] | None = None,
) -> dict:
    """Run an offline comparison without mutating ranking or site state."""
    if k < 1:
        raise ValueError("k must be at least 1")
    selected_variants = tuple(dict.fromkeys(variants))
    unknown = [variant for variant in selected_variants if variant not in VARIANTS]
    if unknown:
        raise ValueError(f"unsupported benchmark variants: {unknown}")
    if BASELINE_VARIANT not in selected_variants:
        raise ValueError("benchmark variants must include bm25_512")
    if not dataset.readiness.ready:
        raise LabelReadinessError(dataset.readiness)

    train_labels, test_labels, holdout_site_id = _select_split(
        dataset,
        split_mode=split_mode,
        site_ids=site_ids,
    )
    if not test_labels:
        raise ReviewerBenchmarkRunError("the selected frozen split has no test labels")

    train_by_site: dict[int, list[ReviewerLabelExample]] = defaultdict(list)
    test_by_site: dict[int, list[ReviewerLabelExample]] = defaultdict(list)
    for label in train_labels:
        train_by_site[label.site_id].append(label)
    for label in test_labels:
        test_by_site[label.site_id].append(label)

    site_results: dict[str, dict] = {}
    all_rankings: dict[str, dict[int, list[int]]] = {variant: {} for variant in selected_variants}
    all_labels: list[ReviewerLabelExample] = []
    for site_id in sorted(test_by_site):
        site_rankings, graph_nodes, training_edges = _rank_site(
            db,
            site_id=site_id,
            cutoff_at=dataset.cutoff_at,
            train_labels=train_by_site.get(site_id, ()),
            test_labels=test_by_site[site_id],
            variants=selected_variants,
        )
        site_labels = tuple(test_by_site[site_id])
        for variant in selected_variants:
            all_rankings[variant].update(site_rankings[variant])
        all_labels.extend(site_labels)

        summaries = {
            variant: evaluate_reviewer_rankings(
                site_rankings[variant],
                site_labels,
                variant=variant,
                k=k,
            )
            for variant in selected_variants
        }
        site_results[str(site_id)] = {
            "test_labels": len(site_labels),
            "train_labels": len(train_by_site.get(site_id, ())),
            "graph_nodes": graph_nodes,
            "training_edges": training_edges,
            "variants": {variant: summary.to_dict() for variant, summary in summaries.items()},
            "comparisons": {
                variant: compare_reviewer_rankings(
                    site_rankings[variant],
                    site_rankings[BASELINE_VARIANT],
                    site_labels,
                    baseline_variant=BASELINE_VARIANT,
                    candidate_variant=variant,
                    k=k,
                ).to_dict()
                for variant in selected_variants
                if variant != BASELINE_VARIANT
            },
        }

    all_summaries = {
        variant: evaluate_reviewer_rankings(
            all_rankings[variant],
            all_labels,
            variant=variant,
            k=k,
        )
        for variant in selected_variants
    }
    return {
        "schema_version": "reviewer_benchmark_v1",
        "dataset_schema_version": dataset.schema_version,
        "readiness": dataset.readiness.to_dict(),
        "split_mode": split_mode,
        "holdout_site_id": holdout_site_id,
        "cutoff_at": dataset.cutoff_at.isoformat(),
        "k": k,
        "selected_site_ids": sorted(test_by_site),
        "train_labels": len(train_labels),
        "test_labels": len(test_labels),
        "baseline_variant": BASELINE_VARIANT,
        "variants": list(selected_variants),
        "generated_at": datetime.now(UTC).isoformat(),
        "build_commit": settings.build_commit or None,
        "sites": site_results,
        "all": {
            "variants": {variant: summary.to_dict() for variant, summary in all_summaries.items()},
            "comparisons": {
                variant: compare_reviewer_rankings(
                    all_rankings[variant],
                    all_rankings[BASELINE_VARIANT],
                    all_labels,
                    baseline_variant=BASELINE_VARIANT,
                    candidate_variant=variant,
                    k=k,
                ).to_dict()
                for variant in selected_variants
                if variant != BASELINE_VARIANT
            },
        },
    }


def _select_split(
    dataset: ReviewerLabelDataset,
    *,
    split_mode: BenchmarkSplit,
    site_ids: Collection[int] | None,
) -> tuple[tuple[ReviewerLabelExample, ...], tuple[ReviewerLabelExample, ...], int | None]:
    if split_mode == "time":
        train = dataset.time_split.train
        test = dataset.time_split.test
        holdout_site_id = None
    elif split_mode == "site_holdout":
        if dataset.site_holdout_split is None:
            raise ReviewerBenchmarkRunError(
                "the frozen dataset has no site_holdout split; freeze it with --holdout-site-id"
            )
        train = dataset.site_holdout_split.train
        test = dataset.site_holdout_split.test
        holdout_site_id = dataset.site_holdout_split.holdout_site_id
    else:
        raise ValueError(f"unsupported split mode: {split_mode!r}")

    if site_ids is None:
        return train, test, holdout_site_id
    selected = set(site_ids)
    return (
        tuple(label for label in train if label.site_id in selected),
        tuple(label for label in test if label.site_id in selected),
        holdout_site_id,
    )


def _rank_site(
    db: Session,
    *,
    site_id: int,
    cutoff_at: datetime,
    train_labels: Sequence[ReviewerLabelExample],
    test_labels: Sequence[ReviewerLabelExample],
    variants: Sequence[str],
) -> tuple[dict[str, dict[int, list[int]]], int, int]:
    source_ids = {label.source_article_id for label in test_labels}
    ranker = EvaluationRanker.load(
        db,
        site_id=site_id,
        model=settings.embedding_model,
        cutoff_at=cutoff_at,
        source_ids=source_ids,
    )
    train_edges = sorted(
        {
            (label.source_article_id, label.target_article_id)
            for label in train_labels
            if label.label == "approved"
        }
    )
    graph = build_as_of_graph(
        db,
        site_id=site_id,
        cutoff_at=cutoff_at,
        source_ids=source_ids,
        train_edges=train_edges,
    )
    features = {feature.article_id: feature for feature in graph.features}
    rankings: dict[str, dict[int, list[int]]] = {variant: {} for variant in variants}
    for source_id in sorted(source_ids):
        candidates = ranker.ranked_candidates(db, source_id=source_id, method="hybrid")
        baseline = [candidate.target_id for candidate in candidates]
        if BASELINE_VARIANT in rankings:
            rankings[BASELINE_VARIANT][source_id] = baseline
        for mode in ("off", "shadow", "active"):
            if mode not in rankings:
                continue
            reranked, _metadata = deterministic_rerank(
                candidates,
                features,
                source_article_id=source_id,
                mode=mode,
                minimum_relevance=settings.suggestion_min_score,
            )
            rankings[mode][source_id] = [candidate.target_id for candidate in reranked]

    if not any(rankings[BASELINE_VARIANT].values()):
        raise ReviewerBenchmarkRunError(
            f"site {site_id} has no current embedded BM25-512 candidates; "
            "refusing to write a zero-valued benchmark"
        )
    return rankings, graph.article_count, graph.edge_count
