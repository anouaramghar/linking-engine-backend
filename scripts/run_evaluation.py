"""Measure suggestion ranking quality against real editorial links.

    uv run --no-sync python scripts/run_evaluation.py --cutoff 2026-01-01T00:00:00+00:00
    uv run --no-sync python scripts/run_evaluation.py --split docs/data/evaluation-split-*.json

Reads the temporal split, asks each ranking method for targets for every source
article published after the cutoff, and scores the answer with recall@k, ndcg@k
and mrr. Read-only: it writes nothing to the database.

Prefer `--split` over `--cutoff`. A rebuilt split moves with the database, so two
runs measure two test sets and the difference between them cannot be read as a
change in ranking quality. `scripts/freeze_evaluation_split.py` writes the file.

Every method is scored on the same sources and the same candidate pool, so the
table compares orderings and nothing else. A new method — the GNN — adds a row
and changes no other part of this measurement.

Only "new source -> old target" examples are measured. A target published after
the cutoff is not in the candidate pool, so the pair is unanswerable by
construction rather than a failure of the ranking.
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.ml.evaluation.metrics import MetricSummary, evaluate_rankings
from app.ml.evaluation.ranking import RANKING_METHODS, EvaluationRanker, RankingMethod
from app.ml.evaluation.temporal_split import (
    TemporalEvaluationSplit,
    build_temporal_evaluation_split,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include a timezone, for example +00:00")
    return parsed


def _load_frozen_split(path: Path) -> TemporalEvaluationSplit:
    return TemporalEvaluationSplit.from_dict(json.loads(path.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--cutoff", type=_aware_datetime, help="rebuild the split from the database"
    )
    source.add_argument(
        "--split", type=Path, help="read a frozen split written by the freeze script"
    )
    parser.add_argument("--ground-truth", choices=("editor", "observed"), default="observed")
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--method",
        action="append",
        choices=RANKING_METHODS,
        dest="methods",
        help="repeatable; defaults to every method",
    )
    parser.add_argument("--output", type=Path, help="write the summaries as JSON")
    args = parser.parse_args()
    methods: tuple[RankingMethod, ...] = tuple(args.methods or RANKING_METHODS)

    with SessionLocal() as db:
        if args.split is not None:
            split = _load_frozen_split(args.split)
            if args.ground_truth != parser.get_default("ground_truth"):
                print(f"note: --ground-truth ignored; the frozen split is {split.ground_truth}")
        else:
            split = build_temporal_evaluation_split(
                db,
                cutoff_at=args.cutoff,
                ground_truth=args.ground_truth,
                site_ids=tuple(args.site_ids) if args.site_ids else None,
            )
        cutoff_at = split.cutoff_at
        measurable = [
            example for example in split.test if example.source_is_new and not example.target_is_new
        ]
        if args.split is not None and args.site_ids:
            # Filtering a frozen split is honest — it drops whole sites, it does not
            # change any site's test set. Rebuilding with --site-id does the same.
            measurable = [
                example for example in measurable if example.site_id in set(args.site_ids)
            ]
        relevant_by_site: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
        for example in measurable:
            relevant_by_site[example.site_id][example.source_article_id].add(
                example.target_article_id
            )

        origin = f"frozen split {args.split}" if args.split is not None else "rebuilt split"
        print(
            f"{origin}  cutoff {cutoff_at:%Y-%m-%d}  ground truth {split.ground_truth}  "
            f"test links {len(split.test)}  measurable {len(measurable)}  "
            f"skipped without publication date {split.skipped_without_publication_date}"
        )
        if not measurable:
            print("nothing to measure")
            return

        # summaries[method][site] — site "all" is added below when more than one site ran.
        summaries: dict[RankingMethod, dict[str, MetricSummary]] = {
            method: {} for method in methods
        }
        all_rankings: dict[RankingMethod, dict[int, list[int]]] = {method: {} for method in methods}
        all_relevant: dict[int, set[int]] = {}
        for site_id, relevant_by_source in sorted(relevant_by_site.items()):
            ranker = EvaluationRanker.load(
                db,
                site_id=site_id,
                model=settings.embedding_model,
                cutoff_at=cutoff_at,
                source_ids=set(relevant_by_source),
            )
            rankings: dict[RankingMethod, dict[int, list[int]]] = {method: {} for method in methods}
            for source_id in relevant_by_source:
                for method, ranked in ranker.rank_all(
                    db, source_id=source_id, methods=methods
                ).items():
                    rankings[method][source_id] = ranked
            print(
                f"\nsite {site_id}: pool {ranker.stats.pool_articles} articles "
                f"({ranker.stats.excluded_low_value} low value removed), "
                f"{ranker.stats.source_articles}/{len(relevant_by_source)} sources embedded"
            )
            for method in methods:
                summaries[method][str(site_id)] = evaluate_rankings(
                    rankings[method], relevant_by_source, k=args.k
                )
                all_rankings[method].update(rankings[method])
            all_relevant.update(relevant_by_source)
            _print_table({method: summaries[method][str(site_id)] for method in methods})

        if len(relevant_by_site) > 1:
            print("\nall sites")
            for method in methods:
                summaries[method]["all"] = evaluate_rankings(
                    all_rankings[method], all_relevant, k=args.k
                )
            _print_table({method: summaries[method]["all"] for method in methods})

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {
                        method: {site: summary.to_dict() for site, summary in by_site.items()}
                        for method, by_site in summaries.items()
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"\nwrote {args.output}")


def _print_table(summaries: dict[RankingMethod, MetricSummary]) -> None:
    """One row per method. Overlapping intervals mean the test set proved nothing."""
    first = next(iter(summaries.values()))
    print(f"  source articles scored: {first.queries}")
    header = f"  {'method':<8}  {f'recall@{first.k}':>9}  {'95% ci':>16}  "
    print(header + f"{f'ndcg@{first.k}':>9}  {'95% ci':>16}  {'mrr':>7}")
    print("  " + "-" * (len(header) + 36))
    for method, summary in summaries.items():
        recall_low, recall_high = summary.recall_ci95
        ndcg_low, ndcg_high = summary.ndcg_ci95
        print(
            f"  {method:<8}  {summary.recall_at_k:>9.4f}  "
            f"{f'[{recall_low:.3f}, {recall_high:.3f}]':>16}  "
            f"{summary.ndcg_at_k:>9.4f}  {f'[{ndcg_low:.3f}, {ndcg_high:.3f}]':>16}  "
            f"{summary.mrr:>7.4f}"
        )


if __name__ == "__main__":
    main()
