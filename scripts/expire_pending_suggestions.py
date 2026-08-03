"""Expire pending suggestions for one site, deliberately.

Generation never does this. The pilot fills only the slots a source has free, so
enabling it cannot silently retire an editor's existing queue — but two
situations still need the queue cleared, and both are operator decisions rather
than side effects:

* freeing capacity before a pilot run, when every source's five slots are
  already occupied by pending baseline rows;
* retiring pending `hybrid_bm25` rows before rolling the pilot back.

The rules this enforces:

* one site per invocation — there is no fleet-wide form, because "all sites"
  is exactly the mistake worth making impossible;
* only `pending` rows are touched. `approved`, `applying`, `applied`, and
  `rejected` are editorial history and are never expired here;
* it prints what it would do and changes nothing unless `--yes` is given.

Usage::

    python -m scripts.expire_pending_suggestions --site-id 12 --method baseline_cosine
    python -m scripts.expire_pending_suggestions --site-id 12 --method hybrid_bm25 --yes
"""

import argparse
import sys

from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.models import Site, Suggestion

#: Never expired by this script: they record a decision somebody made.
PRESERVED_STATUSES = ("approved", "applying", "applied", "rejected")


def _status_counts(db, site_id: int, method: str | None) -> dict[str, int]:
    conditions = [Suggestion.site_id == site_id]
    if method is not None:
        conditions.append(Suggestion.method == method)
    return dict(
        db.execute(
            select(Suggestion.status, func.count())
            .where(*conditions)
            .group_by(Suggestion.status)
        ).all()
    )


def _format(counts: dict[str, int]) -> str:
    return ", ".join(f"{status}={counts[status]}" for status in sorted(counts)) or "none"


def expire_pending(db, *, site_id: int, method: str | None, apply: bool) -> int:
    """Return the number of pending rows expired, or that would be."""
    conditions = [
        Suggestion.site_id == site_id,
        # The whole point: reviewed rows are out of scope, whatever else matches.
        Suggestion.status == "pending",
    ]
    if method is not None:
        conditions.append(Suggestion.method == method)

    if not apply:
        return db.scalar(select(func.count()).select_from(Suggestion).where(*conditions)) or 0

    expired = db.execute(
        update(Suggestion)
        .where(*conditions)
        .values(status="expired")
        .returning(Suggestion.id)
        .execution_options(synchronize_session=False)
    ).all()
    return len(expired)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.expire_pending_suggestions",
        description=(
            "Expire pending suggestions for exactly one site. Reviewed rows "
            "(approved/applying/applied/rejected) are never touched."
        ),
    )
    parser.add_argument(
        "--site-id",
        type=int,
        required=True,
        help="the only site affected; there is no fleet-wide form",
    )
    parser.add_argument(
        "--method",
        choices=("baseline_cosine", "hybrid_bm25", "all"),
        required=True,
        help="which pending rows to expire",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="actually commit; without it the command reports and changes nothing",
    )
    args = parser.parse_args(argv)
    method = None if args.method == "all" else args.method

    with SessionLocal() as db:
        site = db.get(Site, args.site_id)
        if site is None:
            print(f"site {args.site_id} not found", file=sys.stderr)
            return 1

        before = _status_counts(db, args.site_id, method)
        print(f"site {site.id} ({site.name}), method={args.method}")
        print(f"  before: {_format(before)}")

        affected = expire_pending(db, site_id=args.site_id, method=method, apply=args.yes)
        if not args.yes:
            db.rollback()
            print(f"  would expire {affected} pending row(s)")
            print(f"  preserved regardless: {', '.join(PRESERVED_STATUSES)}")
            print("  nothing changed — re-run with --yes to apply")
            return 0

        db.commit()
        print(f"  expired {affected} pending row(s)")
        print(f"  after:  {_format(_status_counts(db, args.site_id, method))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
