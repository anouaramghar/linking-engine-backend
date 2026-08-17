"""Freeze exposed individual reviewer labels for offline ranking evaluation.

The command is deliberately fail-closed:

    uv run --no-sync python scripts/freeze_reviewer_labels.py \
        --cutoff 2026-08-10T00:00:00+00:00 \
        --output docs/data/reviewer-labels.json

The admin API can inspect eligible rows before the threshold. This command will
not write an artifact until three sites each have 100 eligible labels. It does
not fit, promote, or activate a model.
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.db import SessionLocal
from app.ml.evaluation.reviewer_labels import (
    LabelReadinessError,
    ReviewerLabelDataset,
    build_reviewer_label_dataset,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def write_dataset(dataset: ReviewerLabelDataset, output: Path) -> None:
    """Write and read back the artifact before reporting it as frozen."""
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = dataset.to_dict()
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    reloaded = ReviewerLabelDataset.from_dict(
        json.loads(output.read_text(encoding="utf-8"))
    )
    if reloaded != dataset:
        raise RuntimeError(f"{output} did not read back as the dataset that was written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", required=True, type=_aware_datetime)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument("--date-from", type=_aware_datetime)
    parser.add_argument("--date-to", type=_aware_datetime)
    parser.add_argument("--holdout-site-id", type=int)
    args = parser.parse_args()

    try:
        with SessionLocal() as db:
            dataset = build_reviewer_label_dataset(
                db,
                cutoff_at=args.cutoff,
                site_ids=tuple(args.site_ids) if args.site_ids else None,
                date_from=args.date_from,
                date_to=args.date_to,
                holdout_site_id=args.holdout_site_id,
            )
    except LabelReadinessError as error:
        print("reviewer-label artifact not frozen: evidence gate is not ready")
        print(json.dumps(error.readiness.to_dict(), indent=2, sort_keys=True))
        raise SystemExit(2) from error

    write_dataset(dataset, args.output)
    by_site = Counter(row.site_id for row in dataset.labels)
    print(
        f"cutoff {dataset.cutoff_at.isoformat()}  labels {len(dataset.labels)}  "
        f"train {len(dataset.time_split.train)}  test {len(dataset.time_split.test)}"
    )
    for site_id, count in sorted(by_site.items()):
        print(f"  site {site_id}: {count} eligible labels")
    if dataset.site_holdout_split is not None:
        print(
            f"site holdout {dataset.site_holdout_split.holdout_site_id}  "
            f"train {len(dataset.site_holdout_split.train)}  "
            f"test {len(dataset.site_holdout_split.test)}"
        )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
