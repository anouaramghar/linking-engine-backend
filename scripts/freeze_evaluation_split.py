"""Write the temporal evaluation split to a file, so every method is scored on it.

    uv run --no-sync python scripts/freeze_evaluation_split.py \
        --cutoff 2026-01-01T00:00:00+00:00 --ground-truth editor

The split is rebuilt from the database on every run of `run_evaluation.py`, and
the database keeps changing: a crawl adds articles, an editor applies a
suggestion, a site is removed. Two runs a week apart therefore measure two
different test sets, and the difference between them is not an improvement in
ranking. Freezing the split to a file fixes the target once. `run_evaluation.py
--split <path>` then reads it instead of rebuilding it.

Read-only: it writes nothing to the database.
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.db import SessionLocal
from app.ml.evaluation.temporal_split import (
    TemporalEvaluationSplit,
    build_temporal_evaluation_split,
)


DEFAULT_DIRECTORY = Path("docs/data")


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include a timezone, for example +00:00")
    return parsed


def _default_output(cutoff_at: datetime, ground_truth: str) -> Path:
    return DEFAULT_DIRECTORY / f"evaluation-split-{ground_truth}-{cutoff_at:%Y-%m-%d}.json"


def write_split(split: TemporalEvaluationSplit, output: Path) -> None:
    """Write the split, then read it back, so an unloadable file never survives."""
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = split.to_dict()
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    reloaded = TemporalEvaluationSplit.from_dict(json.loads(output.read_text(encoding="utf-8")))
    if reloaded != split:
        raise RuntimeError(f"{output} did not read back as the split that was written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", required=True, type=_aware_datetime)
    parser.add_argument("--ground-truth", choices=("editor", "observed"), default="editor")
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument("--output", type=Path, help="defaults to docs/data/evaluation-split-*.json")
    args = parser.parse_args()

    with SessionLocal() as db:
        split = build_temporal_evaluation_split(
            db,
            cutoff_at=args.cutoff,
            ground_truth=args.ground_truth,
            site_ids=tuple(args.site_ids) if args.site_ids else None,
        )

    # The pairs `run_evaluation.py` can actually score: a source published after the
    # cutoff, a target that already existed. Report it here, because a split with a
    # large `test` and a small measurable count looks healthy and is not.
    measurable = [
        example for example in split.test if example.source_is_new and not example.target_is_new
    ]
    output = args.output or _default_output(args.cutoff, args.ground_truth)
    write_split(split, output)

    print(
        f"cutoff {args.cutoff:%Y-%m-%d}  ground truth {split.ground_truth}  "
        f"train {len(split.train)}  test {len(split.test)}  measurable {len(measurable)}  "
        f"skipped without publication date {split.skipped_without_publication_date}"
    )
    by_site = Counter(example.site_id for example in measurable)
    for site_id, count in sorted(by_site.items()):
        sources = {
            example.source_article_id for example in measurable if example.site_id == site_id
        }
        print(f"  site {site_id}: {count} measurable links over {len(sources)} source articles")
    if not measurable:
        print("  nothing measurable: no link joins a post-cutoff source to a pre-cutoff target")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
