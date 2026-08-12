"""Measure suggestion ranking quality against real editorial links.

    uv run --no-sync python scripts/run_evaluation.py --cutoff 2026-01-01T00:00:00+00:00

Reads the temporal split, asks the ranker for targets for every source article
published after the cutoff, and scores the answer with recall@k, ndcg@k and mrr.
Read-only: it writes nothing to the database.

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
from app.ml.evaluation.metrics import evaluate_rankings
from app.ml.evaluation.ranking import EvaluationRanker
from app.ml.evaluation.temporal_split import build_temporal_evaluation_split


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include a timezone, for example +00:00")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", required=True, type=_aware_datetime)
    parser.add_argument("--ground-truth", choices=("editor", "observed"), default="observed")
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--output", type=Path, help="write the summaries as JSON")
    args = parser.parse_args()

    with SessionLocal() as db:
        split = build_temporal_evaluation_split(
            db,
            cutoff_at=args.cutoff,
            ground_truth=args.ground_truth,
            site_ids=tuple(args.site_ids) if args.site_ids else None,
        )
        measurable = [
            example for example in split.test if example.source_is_new and not example.target_is_new
        ]
        relevant_by_site: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
        for example in measurable:
            relevant_by_site[example.site_id][example.source_article_id].add(
                example.target_article_id
            )

        print(
            f"cutoff {args.cutoff:%Y-%m-%d}  ground truth {split.ground_truth}  "
            f"test links {len(split.test)}  measurable {len(measurable)}  "
            f"skipped without publication date {split.skipped_without_publication_date}"
        )
        if not measurable:
            print("nothing to measure")
            return

        summaries = {}
        all_rankings: dict[int, list[int]] = {}
        all_relevant: dict[int, set[int]] = {}
        for site_id, relevant_by_source in sorted(relevant_by_site.items()):
            ranker = EvaluationRanker.load(
                db,
                site_id=site_id,
                model=settings.embedding_model,
                cutoff_at=args.cutoff,
                source_ids=set(relevant_by_source),
            )
            rankings = {
                source_id: ranker.rank(db, source_id=source_id) for source_id in relevant_by_source
            }
            summary = evaluate_rankings(rankings, relevant_by_source, k=args.k)
            summaries[site_id] = summary
            all_rankings.update(rankings)
            all_relevant.update(relevant_by_source)
            print(
                f"\nsite {site_id}: pool {ranker.stats.pool_articles} articles "
                f"({ranker.stats.excluded_low_value} low value removed), "
                f"{ranker.stats.source_articles}/{len(relevant_by_source)} sources embedded"
            )
            _print_summary(summary)

        if len(summaries) > 1:
            overall = evaluate_rankings(all_rankings, all_relevant, k=args.k)
            summaries["all"] = overall
            print("\nall sites")
            _print_summary(overall)

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {str(key): value.to_dict() for key, value in summaries.items()}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"\nwrote {args.output}")


def _print_summary(summary) -> None:
    low, high = summary.ndcg_ci95
    recall_low, recall_high = summary.recall_ci95
    print(f"  source articles scored : {summary.queries}")
    print(
        f"  recall@{summary.k}               : {summary.recall_at_k:.4f}  "
        f"[{recall_low:.4f}, {recall_high:.4f}]"
    )
    print(f"  ndcg@{summary.k}                 : {summary.ndcg_at_k:.4f}  [{low:.4f}, {high:.4f}]")
    print(f"  mrr                    : {summary.mrr:.4f}")


if __name__ == "__main__":
    main()
