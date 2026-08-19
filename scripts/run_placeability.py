"""Score suggestions by whether a link can physically be placed in the source.

    uv run --no-sync python scripts/run_placeability.py --limit 50
    uv run --no-sync python scripts/run_placeability.py --limit 200 --store

The placement model is already a judge, and nobody is using it as one. It is
asked to quote the source article and choose a contiguous anchor, and it may
decline when no natural position exists. The answer is then verified against the
source text before it is accepted (`app/services/placement_service.py`). A
decline, or an answer that cannot be located, is a defensible automatic verdict
that this suggestion does not belong on this article.

That yields a per-suggestion quality label with no reviewer involved, which
matters because the reviewer-label set is far too small to measure anything
(four `reviewed` events as of 2026-08-19). It is not a substitute for editorial
judgment: a link can be perfectly placeable and still be a poor recommendation.
Read the placeability rate as a *lower bound* on defects, and as an A/B metric
between two ranking configurations measured over the same suggestions.

Cost. One model call per suggestion, several seconds each. `--limit` is
required in spirit if not in syntax; start small.

Writes nothing unless `--store` is passed. With `--store` the generated anchor
and passage are persisted through the service's own first-generation-wins path,
which never touches `status` and never resurrects a stale review.
"""

import argparse
from collections import defaultdict

from sqlalchemy import select

from app.db import SessionLocal
from app.ml.llm.openrouter import OpenRouterError
from app.models import Suggestion
from app.services import placement_service


def _bucket(suggestion: Suggestion) -> str:
    rank = suggestion.final_rank
    return f"final_rank {rank}" if rank else "final_rank unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25, help="suggestions to judge")
    parser.add_argument("--site-id", action="append", type=int, dest="site_ids")
    parser.add_argument(
        "--status",
        action="append",
        dest="statuses",
        help="only these statuses (default: every status)",
    )
    parser.add_argument(
        "--include-placed",
        action="store_true",
        help="re-judge rows that already carry a placement (costs calls, changes nothing)",
    )
    parser.add_argument(
        "--store",
        action="store_true",
        help="persist each generated placement; off by default",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        query = select(Suggestion).order_by(Suggestion.id)
        if args.site_ids:
            query = query.where(Suggestion.site_id.in_(args.site_ids))
        if args.statuses:
            query = query.where(Suggestion.status.in_(args.statuses))
        if not args.include_placed:
            query = query.where(Suggestion.placement_generated_at.is_(None))
        suggestions = db.scalars(query.limit(args.limit)).all()

        if not suggestions:
            print("No suggestion matched. Nothing to judge.")
            return

        print(f"Judging {len(suggestions)} suggestions. One model call each.\n")
        placed = 0
        declined = 0
        errors = 0
        by_bucket: dict[str, list[int]] = defaultdict(list)
        claimed_by_source: dict[int, list[str]] = defaultdict(list)

        for index, suggestion in enumerate(suggestions, start=1):
            # Anchors already claimed on this source, so the judge is not
            # penalized for picking a phrase a sibling suggestion holds.
            taken = list(
                db.scalars(
                    select(Suggestion.anchor_text).where(
                        Suggestion.source_article_id == suggestion.source_article_id,
                        Suggestion.id != suggestion.id,
                        Suggestion.anchor_text.is_not(None),
                    )
                ).all()
            )
            # Anchors handed out earlier in this run count too. Without `--store`
            # they are in no table yet, and leaving them out lets two siblings
            # claim the same phrase -- which publication would never allow, so
            # counting both as placeable would overstate the rate.
            taken.extend(claimed_by_source[suggestion.source_article_id])
            try:
                placement = placement_service.generate(suggestion, taken_anchors=taken)
            except OpenRouterError as error:
                # A model that cannot be reached is a temporary failure. Counting
                # it as "not placeable" would turn an outage into a quality
                # result, which is exactly the mistake this script exists to
                # avoid making.
                errors += 1
                print(f"  [{index}/{len(suggestions)}] id={suggestion.id} UNREACHABLE {error}")
                continue

            usable = placement.anchor_text is not None
            if usable:
                claimed_by_source[suggestion.source_article_id].append(placement.anchor_text)
            placed += usable
            declined += not usable
            by_bucket[_bucket(suggestion)].append(int(usable))
            verdict = f"anchor {placement.anchor_text!r}" if usable else "declined"
            print(f"  [{index}/{len(suggestions)}] id={suggestion.id} {verdict}")

            if args.store:
                placement_service.store(db, suggestion.id, placement)
                db.commit()

        judged = placed + declined
        print("\nPlaceability")
        print(f"  judged        {judged}")
        print(f"  placeable     {placed}" + (f" ({placed / judged:.0%})" if judged else ""))
        print(f"  declined      {declined}" + (f" ({declined / judged:.0%})" if judged else ""))
        if errors:
            print(f"  unreachable   {errors} (excluded from the rate, not counted as failures)")

        if by_bucket:
            print("\nBy delivered position")
            for bucket in sorted(by_bucket):
                values = by_bucket[bucket]
                print(f"  {bucket:22s} {sum(values)}/{len(values)} placeable")

        if not args.store:
            print("\nNothing was written. Pass --store to keep these placements.")


if __name__ == "__main__":
    main()
