"""Compare hybrid ranking with shadow/active graph priorities.

    .\\.venv\\Scripts\\python.exe scripts\\run_graph_evaluation.py `
        --split docs\\data\\evaluation-split-observed-2026-01-01.json `
        --output docs\\data\\evaluation-graph-2026-08-14.json

The frozen split supplies both the test links and the pre-cutoff training links.
The structural metrics simulate the top-K recommendations as a batch; they do not
claim that a reviewer approved or published those links.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.ml.evaluation.graph_metrics import (
    aggregate_structural_outcomes,
    build_as_of_graph,
    evaluate_structural_outcomes,
)
from app.ml.evaluation.metrics import compare_rankings, evaluate_rankings
from app.ml.evaluation.ranking import EvaluationRanker
from app.ml.evaluation.temporal_split import TemporalEvaluationSplit
from app.services.graph_service import deterministic_rerank


VARIANTS = ("hybrid", "shadow", "active")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True, type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be at least 1")

    split = TemporalEvaluationSplit.from_dict(json.loads(args.split.read_text(encoding="utf-8")))
    measurable = [
        example for example in split.test if example.source_is_new and not example.target_is_new
    ]
    if not measurable:
        raise SystemExit(
            "error: the frozen split has no measurable source-to-precutoff-target links"
        )
    relevant_by_site: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    for example in measurable:
        relevant_by_site[example.site_id][example.source_article_id].add(example.target_article_id)

    all_relevant: dict[int, set[int]] = {}
    all_rankings: dict[str, dict[int, list[int]]] = {variant: {} for variant in VARIANTS}
    structural_by_variant: dict[str, list] = {variant: [] for variant in VARIANTS}
    site_results: dict[str, dict] = {}

    with SessionLocal() as db:
        for site_id, relevant_by_source in sorted(relevant_by_site.items()):
            source_ids = set(relevant_by_source)
            ranker = EvaluationRanker.load(
                db,
                site_id=site_id,
                model=settings.embedding_model,
                cutoff_at=split.cutoff_at,
                source_ids=source_ids,
            )
            train_edges = [
                (example.source_article_id, example.target_article_id)
                for example in split.train
                if example.site_id == site_id
            ]
            computation = build_as_of_graph(
                db,
                site_id=site_id,
                cutoff_at=split.cutoff_at,
                source_ids=source_ids,
                train_edges=train_edges,
            )
            if not computation.features:
                raise SystemExit(
                    f"error: site {site_id} from the frozen split is absent from the current "
                    "database; refusing to write a zero-valued report"
                )
            features = {feature.article_id: feature for feature in computation.features}
            rankings = _rank_variants(
                db,
                ranker,
                features,
                source_ids=source_ids,
            )
            if not any(rankings["hybrid"].values()):
                raise SystemExit(
                    f"error: site {site_id} has no embedded evaluation candidates in the "
                    "current database; refusing to write a zero-valued report"
                )
            all_relevant.update(relevant_by_source)
            for variant in VARIANTS:
                all_rankings[variant].update(rankings[variant])

            variants = {}
            for variant in VARIANTS:
                structural = evaluate_structural_outcomes(
                    computation,
                    rankings[variant],
                    k=args.k,
                )
                structural_by_variant[variant].append(structural)
                variants[variant] = {
                    "relevance": evaluate_rankings(
                        rankings[variant], relevant_by_source, k=args.k
                    ).to_dict(),
                    "structural": structural.to_dict(),
                    "comparison_to_hybrid": compare_rankings(
                        rankings[variant],
                        rankings["hybrid"],
                        relevant_by_source,
                        k=args.k,
                    ).to_dict(),
                }
            site_results[str(site_id)] = {
                "queries": len(relevant_by_source),
                "graph_nodes": computation.article_count,
                "training_edges": computation.edge_count,
                "variants": variants,
            }

    all_results = {}
    for variant in VARIANTS:
        all_results[variant] = {
            "relevance": evaluate_rankings(all_rankings[variant], all_relevant, k=args.k).to_dict(),
            "structural": aggregate_structural_outcomes(structural_by_variant[variant]).to_dict(),
            "comparison_to_hybrid": compare_rankings(
                all_rankings[variant],
                all_rankings["hybrid"],
                all_relevant,
                k=args.k,
            ).to_dict(),
        }

    payload = {
        "schema_version": "graph_evaluation_v1",
        "split": str(args.split),
        "ground_truth": split.ground_truth,
        "cutoff_at": split.cutoff_at.isoformat(),
        "test_links": len(split.test),
        "measurable_links": len(measurable),
        "queries": len(all_relevant),
        "baseline_method": "hybrid",
        "sites": site_results,
        "all": all_results,
    }
    _print_report(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.output}")


def _rank_variants(
    db, ranker, features, *, source_ids: set[int]
) -> dict[str, dict[int, list[int]]]:
    rankings = {variant: {} for variant in VARIANTS}
    for source_id in sorted(source_ids):
        candidates = ranker.ranked_candidates(db, source_id=source_id, method="hybrid")
        rankings["hybrid"][source_id] = [candidate.target_id for candidate in candidates]
        for mode in ("shadow", "active"):
            reranked, _metadata = deterministic_rerank(
                candidates,
                features,
                source_article_id=source_id,
                mode=mode,
                minimum_relevance=settings.suggestion_min_score,
            )
            rankings[mode][source_id] = [candidate.target_id for candidate in reranked]
    return rankings


def _print_report(payload: dict) -> None:
    print(
        f"frozen split {payload['split']}  cutoff {payload['cutoff_at'][:10]}  "
        f"ground truth {payload['ground_truth']}  test links {payload['test_links']}  "
        f"measurable {payload['measurable_links']}  queries {payload['queries']}"
    )
    for site_id, result in payload["sites"].items():
        print(
            f"\nsite {site_id}: graph nodes {result['graph_nodes']}  "
            f"training edges {result['training_edges']}  queries {result['queries']}"
        )
        _print_variant_table(result["variants"])
        _print_structural_table(result["variants"])
    print("\nall sites")
    _print_variant_table(payload["all"])
    _print_structural_table(payload["all"])


def _print_variant_table(variants: dict) -> None:
    first = variants["hybrid"]["relevance"]
    k = first["k"]
    print(f"  {'variant':<9}  {f'recall@{k}':>9}  {f'ndcg@{k}':>9}  {'mrr':>7}")
    print("  " + "-" * 43)
    for variant in VARIANTS:
        summary = variants[variant]["relevance"]
        print(
            f"  {variant:<9}  {summary['recall_at_k']:>9.4f}  "
            f"{summary['ndcg_at_k']:>9.4f}  {summary['mrr']:>7.4f}"
        )


def _print_structural_table(variants: dict) -> None:
    print(
        f"  {'variant':<9}  {'orphan delta':>12}  {'underlinked delta':>17}  "
        f"{'saturated delta':>15}  {'newly connected':>16}  {'concentration':>14}"
    )
    print("  " + "-" * 89)
    for variant in VARIANTS:
        structural = variants[variant]["structural"]
        delta = structural["delta"]
        print(
            f"  {variant:<9}  {delta['orphan_count']:>9}  "
            f"{delta['underlinked_count']:>14}  {delta['saturated_count']:>12}  "
            f"{structural['newly_connected_count']:>16}  "
            f"{structural['target_concentration']:>13.1%}"
        )


if __name__ == "__main__":
    main()
