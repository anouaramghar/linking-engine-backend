"""Compare BM25-512 with deterministic graph variants on frozen reviewer labels.

    uv run --no-sync python scripts/run_reviewer_label_benchmark.py \
        --split docs/data/reviewer-labels.json \
        --split-mode time \
        --output docs/data/reviewer-benchmark-2026-08-14.json

The dataset must have passed the three-site evidence gate when it was frozen.
This command is read-only: it does not change graph mode, ranking settings, or
the suggestion queue. It reports active reranking as a comparison only.
"""

import argparse
import json
from pathlib import Path

from app.db import SessionLocal
from app.ml.evaluation.reviewer_benchmark_runner import (
    VARIANTS,
    ReviewerBenchmarkRunError,
    run_reviewer_benchmark,
)
from app.ml.evaluation.reviewer_labels import FrozenReviewerLabelError, ReviewerLabelDataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True, type=Path)
    parser.add_argument("--split-mode", choices=("time", "site_holdout"), default="time")
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--variant",
        action="append",
        choices=VARIANTS,
        dest="variants",
        help="repeatable; defaults to BM25-512, off, shadow, and active",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be at least 1")

    try:
        dataset = ReviewerLabelDataset.from_dict(
            json.loads(args.split.read_text(encoding="utf-8"))
        )
        with SessionLocal() as db:
            payload = run_reviewer_benchmark(
                db,
                dataset,
                split_mode=args.split_mode,
                k=args.k,
                variants=tuple(args.variants or VARIANTS),
                site_ids=tuple(args.site_ids) if args.site_ids else None,
            )
    except (FrozenReviewerLabelError, ReviewerBenchmarkRunError, ValueError) as error:
        raise SystemExit(f"error: {error}") from error

    _print_report(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.output}")


def _print_report(payload: dict) -> None:
    print(
        f"reviewer-label split {payload['split_mode']}  cutoff {payload['cutoff_at'][:10]}  "
        f"test labels {payload['test_labels']}  sites {len(payload['selected_site_ids'])}"
    )
    for site_id, result in payload["sites"].items():
        print(
            f"\nsite {site_id}: test labels {result['test_labels']}  "
            f"train labels {result['train_labels']}  graph nodes {result['graph_nodes']}"
        )
        _print_variant_table(result["variants"])
    print("\nall sites")
    _print_variant_table(payload["all"]["variants"])
    print("\ncomparison to bm25_512")
    for variant, comparison in payload["all"]["comparisons"].items():
        print(
            f"  {variant:<6}  ndcg delta {comparison['ndcg_delta']!s:<9}  "
            f"label precision delta {comparison['label_precision_delta']!s:<9}  "
            f"rejected-rate delta {comparison['rejected_rate_delta']!s}"
        )


def _print_variant_table(variants: dict) -> None:
    first = next(iter(variants.values()))
    k = first["k"]
    print(
        f"  {'variant':<10}  {'ndcg@' + str(k):>9}  {'mrr':>7}  "
        f"{'label p@' + str(k):>12}  {'reject@' + str(k):>12}  {'coverage':>10}"
    )
    print("  " + "-" * 70)
    for variant, summary in variants.items():
        print(
            f"  {variant:<10}  {summary['ndcg_at_k']!s:>9}  {summary['mrr']!s:>7}  "
            f"{summary['label_precision_at_k']!s:>12}  "
            f"{summary['rejected_rate_at_k']!s:>12}  "
            f"{summary['label_coverage']:.3f}"
        )


if __name__ == "__main__":
    main()
