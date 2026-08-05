import argparse
import json
from datetime import datetime
from pathlib import Path

from app.db import SessionLocal
from app.ml.evaluation.temporal_split import build_temporal_evaluation_split


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include a timezone, for example +00:00")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a deterministic time-based link evaluation dataset."
    )
    parser.add_argument("--cutoff", required=True, type=_aware_datetime)
    parser.add_argument("--ground-truth", choices=("editor", "observed"), default="editor")
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    with SessionLocal() as db:
        split = build_temporal_evaluation_split(
            db,
            cutoff_at=args.cutoff,
            ground_truth=args.ground_truth,
            site_ids=tuple(args.site_ids) if args.site_ids else None,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(split.to_dict(), indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {len(split.train)} train and {len(split.test)} test examples "
        f"to {args.output}"
    )


if __name__ == "__main__":
    main()
