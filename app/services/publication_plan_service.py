"""Everything that decides what publication writes, and the proof it was approved.

The rule this module exists to enforce: **no model call, placement choice, target
expansion, fallback choice, or HTML rendering may happen after final approval.**

So every one of those decisions happens here, during preparation, in front of an
operator. Preparation selects the cohort (reciprocal suppression, anchor
arbitration, ordering), fills in missing placements, reads the live WordPress
post, renders the exact resulting HTML, and stores all of it as a `PublicationPlan`
with a SHA-256 hash over the whole artifact. Approval binds a named human to that
hash. The worker then has nothing left to decide — it sends stored bytes.

The hash is recomputed from persisted fields at approval *and* at publication, and
never merely read back from `plan_hash`: a column that would be rewritten by the
same statement that tampered with the artifact proves nothing.
"""

import hashlib
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, joinedload

from app.config import settings
from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.ml.llm import openrouter
from app.models import PublicationPlan, Suggestion
from app.models.publication_plan import ACTIVE_PLAN_STATUSES, PLAN_SCHEMA_VERSION
from app.services import placement_service
from app.services.job_service import record_progress_durably

logger = logging.getLogger(__name__)

#: Bounded so one broken post explains itself in the response and in the row,
#: without a stack trace becoming a database column.
MAX_REASON_CHARS = 500


class PlanIntegrityError(Exception):
    """A stored artifact no longer hashes to the value bound to it.

    Never retryable and never a reason to write: the bytes an operator approved
    and the bytes in the row have diverged, and only a human can say which was
    meant.
    """


class PlanApprovalError(Exception):
    """An approval request does not describe exactly what is on the server.

    Carries the reason so the route can return it as a 409 body. Any single
    mismatch fails the whole request — approving "the ones that still match" is
    approving a set the operator never saw.
    """


@dataclass
class PreparationError:
    """One source article deliberately left out of this batch."""

    source_article_id: int
    source_url: str
    message: str


@dataclass
class PublicationPreparation:
    """What an operator may now approve, and what was left out.

    `has_more` means more source articles remain unshown. It never means they
    will be swept into the current approval.
    """

    site_id: int
    selected_suggestions: int
    plans: list[PublicationPlan] = field(default_factory=list)
    errors: list[PreparationError] = field(default_factory=list)
    has_more: bool = False


# -- cohort selection ------------------------------------------------------
#
# Everything from here to `generate_missing_placements` used to live in the
# publication worker, which is exactly why publication could not be trusted: the
# batch, its order, and its anchors were decided after the last human had left.


def _bm25(suggestion_row) -> float:
    """The score that actually chose a Hybrid row, for arbitrating one anchor.

    `Suggestion.score` is cosine for every method, deliberately, so the queue
    keeps one meaning — but cosine had no part in selecting a hybrid row, and
    letting it decide which of two suggestions wins a contested phrase is an
    arbitrary tiebreak dressed up as a ranking. BM25 is not comparable *across*
    source articles, which is exactly why it is used only here: every suggestion
    being compared shares one source, so it is the same query document.
    """
    components = suggestion_row.score_components
    value = components.get("bm25_score") if isinstance(components, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def _publication_rank(row) -> tuple[float, float, int]:
    """Sort key for one suggestion: BM25, then cosine, then id. Lower is better."""
    return (-_bm25(row), -row.score, row.id)


def _direction_rank(row) -> tuple[float, int]:
    """Which way round to write a reciprocal pair. Lower wins.

    There is no honest ranking to make here: cosine is symmetric, so A->B and
    B->A carry the *same* score, and BM25 is not comparable across the two
    source articles. This is a deterministic arbitrary choice, and saying so is
    better than dressing the id tiebreak up as a measurement.
    """
    return (-row.score, row.id)


def grouped_batch(db: Session, site_id: int) -> tuple[list[tuple[int, list[int]]], list[int]]:
    """Selected suggestions as (source_article_id, suggestion_ids), best first,
    plus the ids whose reverse direction is already a published link.

    Within an article, order *is* the anchor arbitration: the first suggestion to
    render takes the phrase and the rest fall back to the appended block.
    Articles themselves stay in best-cosine-first order so a partial preparation
    still shows the strongest work.

    Near-duplicate template pages propose A->B and B->A together and a bulk
    selection takes both, which would write a mutual link neither page needs. The
    queue's reciprocal filter only ever hid one of them from view; this is the
    gate that stops the second from being rendered. A suppressed row is returned
    rather than quietly dropped, because a row that can never publish and stays
    selected is counted as awaiting publication for ever.

    Only a reverse that is already *applied* is reported as superseded. The loser
    of an in-batch pair stays selected until its winner is published; a later
    preparation then expires it.
    """
    reverse = aliased(Suggestion)
    rows = db.execute(
        select(
            Suggestion.id,
            Suggestion.source_article_id,
            Suggestion.target_article_id,
            Suggestion.score,
            Suggestion.score_components,
            exists(
                select(1).where(
                    reverse.source_article_id == Suggestion.target_article_id,
                    reverse.target_article_id == Suggestion.source_article_id,
                    reverse.status == "applied",
                )
            ).label("reverse_applied"),
        ).where(
            Suggestion.site_id == site_id,
            Suggestion.status == "approved",
            # A row already bound to an approved plan is spoken for. Re-selecting
            # it into a second plan would let one suggestion publish twice.
            Suggestion.publication_plan_id.is_(None),
        )
    ).all()
    superseded = [row.id for row in rows if row.reverse_applied]
    rows = [row for row in rows if not row.reverse_applied]

    # The reverse may also be sitting in this very batch, where no database
    # state has ruled on it yet. Only the *opposite* direction is dropped: two
    # rows for the same pair pointing the same way are one link proposed twice,
    # which the connector's exact-href check already reports as already_present.
    by_pair: dict[frozenset, list] = defaultdict(list)
    for row in rows:
        by_pair[frozenset((row.source_article_id, row.target_article_id))].append(row)
    kept: set[int] = set()
    for pair_rows in by_pair.values():
        if len({row.source_article_id for row in pair_rows}) < 2:
            kept.update(row.id for row in pair_rows)
            continue
        winner = min(pair_rows, key=_direction_rank).source_article_id
        kept.update(row.id for row in pair_rows if row.source_article_id == winner)

    by_source: dict[int, list] = defaultdict(list)
    for row in rows:
        if row.id in kept:
            by_source[row.source_article_id].append(row)
    groups = []
    for source_article_id, source_rows in by_source.items():
        source_rows.sort(key=_publication_rank)
        groups.append((source_article_id, [row.id for row in source_rows], source_rows[0].score))
    groups.sort(key=lambda group: (-group[2], group[0]))
    return [(source_article_id, ids) for source_article_id, ids, _score in groups], superseded


def expire_superseded(db: Session, suggestion_ids: list[int]) -> int:
    """Retire selections whose link already exists the other way round.

    Terminal, not skipped: such a row can never publish, and left selected it is
    reported as awaiting publication by every preparation for ever while nothing
    about it changes.
    """
    if not suggestion_ids:
        return 0
    return db.execute(
        update(Suggestion)
        .where(
            Suggestion.id.in_(suggestion_ids),
            Suggestion.status == "approved",
            Suggestion.publication_plan_id.is_(None),
        )
        .values(status="expired")
        .execution_options(synchronize_session=False)
    ).rowcount


def generate_missing_placements(
    groups: list[tuple[int, list[int]]], job_run_id: int | None = None
) -> int:
    """Fill in placements for selected suggestions that never had a drawer opened.

    Generation is lazy by design: an analysis run produces far more suggestions
    than an editor ever opens, and paying per row was rejected. But the real
    workflow is bulk selection, so without this pass almost every prepared link
    would render as the appended block — the feature would exist and never fire.

    This is the *last* moment a model may be called. After the plan is rendered
    and approved, a placement generated later cannot change it: the block an
    operator saw stays the block that is published.

    Runs on its own session, before any rendering, so a multi-second external
    request is never held across a transaction. Suggestions are grouped by source
    article so each call can be told which anchors its siblings already took.
    """
    budget = settings.publish_max_placement_calls_per_run
    if not budget or not openrouter.is_configured():
        return 0

    db = SessionLocal()
    try:
        pending: list[tuple[list[Suggestion], list[str]]] = []
        spent = 0
        for _source_article_id, suggestion_ids in groups:
            if spent >= budget:
                break
            rows = db.scalars(
                select(Suggestion)
                .where(Suggestion.id.in_(suggestion_ids))
                .options(
                    joinedload(Suggestion.source_article),
                    joinedload(Suggestion.target_article),
                )
            ).all()
            by_id = {row.id: row for row in rows}
            ordered = [by_id[sid] for sid in suggestion_ids if sid in by_id]
            missing = [row for row in ordered if row.placement_generated_at is None]
            if not missing:
                continue
            # The budget is spent here rather than mid-flight so the pass is
            # deterministic and parallel workers need no shared counter.
            missing = missing[: budget - spent]
            spent += len(missing)
            pending.append(
                (
                    missing,
                    [row.anchor_text for row in ordered if row.anchor_text],
                )
            )
        if not pending:
            return 0
        # Detach before the model calls: everything needed is loaded, and an open
        # read transaction held across a multi-second external request sits in
        # front of this same process's writes.
        db.expunge_all()
        db.rollback()

        # One source article at a time within a group, because each call has to
        # know which anchors its siblings just claimed. Different articles share
        # nothing, so they overlap: the pass is latency-bound on an external API,
        # and 500 sequential calls at ~5s each would not fit a request timeout.
        with ThreadPoolExecutor(max_workers=settings.publish_placement_concurrency) as pool:
            results = list(pool.map(_generate_for_source, pending))

        generated = 0
        for source_results in results:
            for suggestion_id, placement in source_results:
                # Counts what the row ends up holding, not what this pass
                # generated: a concurrently stored placement keeps its own
                # answer, and reporting ours would overstate the pass.
                stored_placement = placement_service.store(db, suggestion_id, placement)
                generated += bool(stored_placement.anchor_text)
        db.commit()
        if spent >= budget:
            logger.warning(
                "preparation stopped at %s placement calls; the rest are prepared "
                "as the appended block",
                budget,
            )
        record_progress_durably(
            job_run_id,
            placements_generated=generated,
            placement_calls=spent,
        )
        return generated
    finally:
        db.close()


def _generate_for_source(
    work: tuple[list[Suggestion], list[str]],
) -> list[tuple[int, placement_service.Placement]]:
    """Generate placements for one source article, in order, sharing its anchors.

    No database work: the suggestions are detached and the answers are stored by
    the caller on its own session. A failure is logged and dropped rather than
    raised — an unreachable model is a reason to prepare the appended block, not
    a reason to fail the whole preparation.
    """
    missing, taken = work
    results = []
    for suggestion in missing:
        try:
            placement = placement_service.generate(suggestion, taken_anchors=taken)
        except Exception:
            logger.exception("placement generation failed for suggestion %s", suggestion.id)
            continue
        results.append((suggestion.id, placement))
        if placement.anchor_text:
            taken.append(placement.anchor_text)
    return results


# -- the artifact and its hash ---------------------------------------------


def canonical_artifact(plan) -> dict:
    """The complete logical object the hash covers.

    `schema_version` is inside the hash, not beside it, so an artifact
    serialized under an older shape can never collide with a newer one. `items`
    is hashed exactly as stored, list order included — normalizing it here would
    let an extra key or a reordering slip through unnoticed, which is precisely
    the tampering this is meant to catch.
    """
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "site_id": plan.site_id,
        "source_article_id": plan.source_article_id,
        "source_url": plan.source_url,
        "original_html": plan.original_html,
        "updated_html": plan.updated_html,
        "items": plan.items,
    }


def compute_plan_hash(plan) -> str:
    """SHA-256 over the canonical serialization of `plan`'s artifact.

    `sort_keys` and the compact separators make the encoding depend on the values
    alone, not on dict insertion order or on Python's default spacing.
    `ensure_ascii=False` keeps non-ASCII content as its own UTF-8 bytes rather
    than as escapes, so the hash is stable across json library versions.
    """
    payload = json.dumps(
        canonical_artifact(plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_integrity(plan: PublicationPlan) -> None:
    """Refuse a plan whose stored artifact no longer matches its own hash.

    Recomputed rather than trusted, and compared against `approved_hash` as well
    once a human is bound to it: an UPDATE that rewrote `original_html` would
    rewrite `plan_hash` in the same statement just as easily.
    """
    recomputed = compute_plan_hash(plan)
    if recomputed != plan.plan_hash:
        raise PlanIntegrityError(
            f"publication plan {plan.id} does not match its stored hash "
            f"(recomputed {recomputed[:12]}…, stored {str(plan.plan_hash)[:12]}…)"
        )
    if plan.approved_hash is not None and plan.approved_hash != recomputed:
        raise PlanIntegrityError(
            f"publication plan {plan.id} no longer matches the artifact its operator "
            f"approved (approved {str(plan.approved_hash)[:12]}…, now {recomputed[:12]}…)"
        )


# -- preparation -----------------------------------------------------------


def snapshot_items(ordered: list[Suggestion], outcomes: list[str]) -> list[dict]:
    """One immutable record per link, in the order it was rendered.

    `position` is explicit as well as implied by the list, because the order is
    the anchor arbitration: reordering these two entries is a different edit even
    when every other field is identical.
    """
    return [
        {
            "position": position,
            "suggestion_id": suggestion.id,
            "target_url": suggestion.resolved_target_url,
            "anchor_text": suggestion.anchor_text,
            "outcome": outcome,
        }
        for position, (suggestion, outcome) in enumerate(zip(ordered, outcomes, strict=True))
    ]


def _active_plan(
    db: Session, source_article_id: int, *, lock: bool = False
) -> PublicationPlan | None:
    query = (
        select(PublicationPlan)
        .where(
            PublicationPlan.source_article_id == source_article_id,
            PublicationPlan.status.in_(ACTIVE_PLAN_STATUSES),
        )
        .execution_options(populate_existing=lock)
    )
    if lock:
        query = query.with_for_update()
    return db.scalars(query).first()


def _supersede(db: Session, plan: PublicationPlan, reason: str) -> None:
    plan.status = "superseded"
    plan.invalidated_at = datetime.now(timezone.utc)
    plan.failure_reason = reason[:MAX_REASON_CHARS]


def prepare_site(
    db: Session,
    site,
    *,
    max_articles: int,
    job_run_id: int | None = None,
) -> PublicationPreparation:
    """Render and persist the exact edits an operator may now approve.

    Preparation is allowed to spend money and to read the customer's site: it
    generates missing placements and performs one WordPress GET per source
    article. It writes nothing back to WordPress, approves nothing, and changes
    no suggestion's review status.

    An unreachable source yields an error and no plan, so a dead post cannot
    quietly ride along in someone else's approval.
    """
    groups, superseded = grouped_batch(db, site.id)
    if superseded and expire_superseded(db, superseded):
        db.commit()
        logger.info(
            "expired %s selected suggestion(s) on site %s already linked the other way",
            len(superseded),
            site.id,
        )

    selected = (
        db.scalar(
            select(func.count())
            .select_from(Suggestion)
            .where(
                Suggestion.site_id == site.id,
                Suggestion.status == "approved",
                Suggestion.publication_plan_id.is_(None),
            )
        )
        or 0
    )

    preparation = PublicationPreparation(
        site_id=site.id,
        selected_suggestions=selected,
        has_more=len(groups) > max_articles,
    )
    batch = groups[:max_articles]
    if job_run_id is not None:
        record_progress_durably(
            job_run_id,
            stage="preparing",
            completed=0,
            total=len(batch),
        )
    if not batch:
        return preparation

    # The last model call of the whole lifecycle, and it happens here, before
    # anything is rendered or shown.
    generate_missing_placements([(source, ids) for source, ids in batch])
    db.expire_all()

    connector = get_connector(site)
    for completed, (source_article_id, suggestion_ids) in enumerate(batch, start=1):
        plan = _prepare_one(db, site, connector, source_article_id, suggestion_ids, preparation)
        if plan is not None:
            preparation.plans.append(plan)
        if job_run_id is not None:
            record_progress_durably(
                job_run_id,
                stage="preparing",
                completed=completed,
                total=len(batch),
            )
    return preparation


def _prepare_one(
    db: Session,
    site,
    connector,
    source_article_id: int,
    suggestion_ids: list[int],
    preparation: PublicationPreparation,
) -> PublicationPlan | None:
    """One source article: read it live, render it, store the frozen result."""
    rows = db.scalars(
        select(Suggestion)
        .where(Suggestion.id.in_(suggestion_ids))
        .options(
            joinedload(Suggestion.source_article),
            joinedload(Suggestion.target_article),
        )
    ).all()
    by_id = {row.id: row for row in rows}
    ordered = [by_id[sid] for sid in suggestion_ids if sid in by_id]
    if not ordered:
        preparation.errors.append(
            PreparationError(
                source_article_id=source_article_id,
                source_url="",
                message="the selected suggestions changed while this batch was being prepared",
            )
        )
        return None

    source = ordered[0].source_article
    existing = _active_plan(db, source_article_id)
    if existing is not None and existing.status == "approved":
        # Never supersede an artifact a human is already bound to. This source
        # rejoins preparation once its plan is applied, stale, or failed.
        preparation.errors.append(
            PreparationError(
                source_article_id=source_article_id,
                source_url=source.url,
                message=(
                    f"an approved plan ({existing.id}) already covers this article; "
                    "queue or resolve it before preparing another"
                ),
            )
        )
        return None

    try:
        preview = connector.preview_links(ordered)
    except Exception as error:
        # One unreachable post must not lose the preparation of the others.
        logger.warning("preparation failed for source article %s: %s", source_article_id, error)
        preparation.errors.append(
            PreparationError(
                source_article_id=source_article_id,
                source_url=source.url,
                message=str(error)[:MAX_REASON_CHARS],
            )
        )
        db.rollback()
        return None

    plan = PublicationPlan(
        site_id=site.id,
        source_article_id=source_article_id,
        source_url=source.url,
        status="prepared",
        original_html=preview.original_content,
        updated_html=preview.updated_content,
        items=snapshot_items(ordered, preview.outcomes),
    )
    plan.plan_hash = compute_plan_hash(plan)

    # Previewing performs network I/O. The plan that was prepared when it began
    # may have been approved while that request was in flight, so re-read it
    # under a row lock before deciding whether it may be superseded.
    existing = _active_plan(db, source_article_id, lock=True)
    if existing is not None and existing.status == "approved":
        preparation.errors.append(
            PreparationError(
                source_article_id=source_article_id,
                source_url=source.url,
                message=(
                    f"an approved plan ({existing.id}) already covers this article; "
                    "queue or resolve it before preparing another"
                ),
            )
        )
        db.rollback()
        return None

    still_selected = set(
        db.scalars(
            select(Suggestion.id)
            .where(
                Suggestion.id.in_(suggestion_ids),
                Suggestion.status == "approved",
                Suggestion.publication_plan_id.is_(None),
            )
            .order_by(Suggestion.id)
            .with_for_update()
        )
    )
    if still_selected != set(suggestion_ids):
        preparation.errors.append(
            PreparationError(
                source_article_id=source_article_id,
                source_url=source.url,
                message="the selected suggestions changed while this batch was being prepared",
            )
        )
        db.rollback()
        return None

    if existing is not None:
        if existing.plan_hash == plan.plan_hash:
            # Re-previewing an unchanged article must not churn the row an
            # operator may already be looking at, or invalidate its hash.
            return existing
        _supersede(db, existing, "replaced by a newer preparation of the same article")
        # Flushed on its own: the unit of work emits inserts before updates, so
        # without this the replacement row meets the active-plan unique index
        # while the row it replaces is still 'prepared'.
        db.flush()

    db.add(plan)
    try:
        db.commit()
    except IntegrityError:
        # Another preparation claimed this source article between the read above
        # and this insert. The active-plan index is the arbiter, not this process.
        db.rollback()
        logger.warning("concurrent preparation for source article %s", source_article_id)
        preparation.errors.append(
            PreparationError(
                source_article_id=source_article_id,
                source_url=source.url,
                message="another preparation is already active for this article; reload and retry",
            )
        )
        return None
    db.refresh(plan)
    return plan


# -- approval --------------------------------------------------------------


def approve_plans(
    db: Session,
    site_id: int,
    approvals: list[tuple[int, str]],
    *,
    approved_by: str,
) -> list[PublicationPlan]:
    """Bind a named human to exactly these artifacts, or approve none of them.

    `approvals` is (plan id, plan hash) as the operator's screen showed them.
    Every plan is locked, re-read, re-hashed, and checked against its site, its
    status, and the hash the client sent; every snapshotted suggestion must still
    be selected and unattached. One mismatch fails the request, because approving
    the subset that still matches is approving a set nobody looked at.
    """
    if not approvals:
        raise PlanApprovalError("an approval must name at least one publication plan")

    wanted = dict(approvals)
    plans = db.scalars(
        select(PublicationPlan)
        .where(PublicationPlan.id.in_(wanted))
        # Ordered inside the lock so two concurrent approvals of an overlapping
        # set cannot deadlock by taking the same rows in opposite orders.
        .order_by(PublicationPlan.id)
        # Sessions do not expire on commit, so an instance already in the
        # identity map would otherwise be checked as it was read earlier rather
        # than as it is under the lock.
        .execution_options(populate_existing=True)
        .with_for_update()
    ).all()
    found = {plan.id for plan in plans}
    missing = sorted(set(wanted) - found)
    if missing:
        raise PlanApprovalError(f"publication plan(s) {missing} do not exist")

    for plan in plans:
        if plan.site_id != site_id:
            raise PlanApprovalError(f"publication plan {plan.id} belongs to another site")
        if plan.status != "prepared":
            raise PlanApprovalError(
                f"publication plan {plan.id} is {plan.status}, not prepared; "
                "reload the preparation and approve the current artifact"
            )
        if plan.plan_hash != wanted[plan.id]:
            raise PlanApprovalError(
                f"publication plan {plan.id} has changed since it was shown; "
                "reload the preparation and review the new edit"
            )
        try:
            verify_integrity(plan)
        except PlanIntegrityError as error:
            raise PlanApprovalError(str(error)) from error

    approved_at = datetime.now(timezone.utc)
    snapshotted = {item["suggestion_id"]: plan for plan in plans for item in (plan.items or [])}
    if snapshotted:
        suggestions = db.scalars(
            select(Suggestion)
            .where(Suggestion.id.in_(snapshotted))
            .order_by(Suggestion.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
        by_id = {row.id: row for row in suggestions}
        for suggestion_id, plan in snapshotted.items():
            row = by_id.get(suggestion_id)
            if row is None:
                raise PlanApprovalError(
                    f"suggestion {suggestion_id} in plan {plan.id} no longer exists"
                )
            if row.status != "approved":
                raise PlanApprovalError(
                    f"suggestion {suggestion_id} in plan {plan.id} is now {row.status}; "
                    "reload the preparation"
                )
            if row.publication_plan_id is not None:
                raise PlanApprovalError(
                    f"suggestion {suggestion_id} in plan {plan.id} already belongs to "
                    f"plan {row.publication_plan_id}"
                )

        for suggestion_id, plan in snapshotted.items():
            by_id[suggestion_id].publication_plan_id = plan.id

    for plan in plans:
        plan.status = "approved"
        plan.approved_hash = plan.plan_hash
        plan.approved_by = approved_by
        plan.approved_at = approved_at
    db.commit()
    for plan in plans:
        db.refresh(plan)
    return plans


# -- consumption -----------------------------------------------------------


def load_approved_plans(
    db: Session, site_id: int, *, plan_ids: list[int] | None = None
) -> list[PublicationPlan]:
    """The only cohort publication is allowed to work from."""
    query = select(PublicationPlan).where(
        PublicationPlan.site_id == site_id,
        PublicationPlan.status == "approved",
    )
    if plan_ids is not None:
        query = query.where(PublicationPlan.id.in_(plan_ids))
    return list(db.scalars(query.order_by(PublicationPlan.id)).all())


def mark_stale(db: Session, plan: PublicationPlan, reason: str) -> None:
    """The live article no longer matches what was approved, so nothing is written.

    The suggestions go back to being merely selected — they are still good
    editorial decisions, they just need a new rendering and a new approval
    against the article as it is now. Committed here rather than left to the
    caller: the worker continues with other plans afterwards, and a rollback
    somewhere later must not resurrect an artifact already known to be wrong.
    """
    plan.status = "stale"
    plan.invalidated_at = datetime.now(timezone.utc)
    plan.failure_reason = reason[:MAX_REASON_CHARS]
    db.execute(
        update(Suggestion)
        .where(Suggestion.publication_plan_id == plan.id)
        .values(publication_plan_id=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
