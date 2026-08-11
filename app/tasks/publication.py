"""Publication worker (sequence 4.3): apply approved links via each site's connector.

Safe publication (Phase 0, finding 5): each source article is claimed atomically
(approved -> applying) inside a transaction that also holds a per-source-article
advisory lock for the whole remote write. Concurrent workers cannot double-publish,
writes to one source article are serialized, and a reviewer rejecting mid-flight
blocks on the row lock and gets a clean 409 once the publish commits. Any failure
or crash rolls the claim back to 'approved' for retry (A8); apply_links's exact-href
check keeps retries idempotent when WordPress succeeded but the commit didn't.

Work is batched **per source article**, not per suggestion: three links into one
post used to cost three GETs, three POSTs and three WordPress revisions. One
article is now one read, one edit and one revision, which is also what makes the
in-text limit and the anchor-conflict rule observable in a single pass.

Before any write, a preflight pass fills in placements the review queue never
generated. Placement is lazy by design — it costs a model call and most
suggestions are never opened — but the real workflow is bulk approval, so
without this almost every published link falls back to the appended block.

A suggestion failure does not stop later batch entries; after the loop, any
failure is re-raised so RQ retries it and terminal failures produce durable alerts.
A suggestion that keeps failing past `publish_max_suggestion_attempts` is moved to
'failed' instead of being retried by every future run. Publication never writes
internal_links — the applied link is detected at the next crawl (A9, ingestion is
the single source of truth). Durable accounting spans all attempts while retaining
latest-attempt failure and skip diagnostics.
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import sleep

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import aliased, joinedload

from app.config import settings
from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.ml.llm import openrouter
from app.models import Article, JobRun, Site, Suggestion, SuggestionEvent
from app.services import placement_service
from app.services.publication_progress import (
    begin_publication_attempt,
    complete_publication_success,
    record_publication_applied,
    record_publication_failure,
    record_publication_skip,
    resume_outcome_counts,
)
from app.services.external_link_policy import expire_ineligible_external_suggestions
from app.services.job_service import (
    NonRetryableTaskError,
    record_progress,
    record_progress_durably,
    run_durably,
)

logger = logging.getLogger(__name__)

_PUBLISH_LOCK_NAMESPACE = 0x4C50  # "LP" - keyed by source article; see namespace registry


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


def grouped_batch(db, site_id: int) -> tuple[list[tuple[int, list[int]]], list[int]]:
    """Approved suggestions as (source_article_id, suggestion_ids), best first,
    plus the ids whose reverse direction is already a published link.

    Within an article, publication order *is* the anchor arbitration: the first
    suggestion to write takes the phrase and the rest fall back to the appended
    block. Articles themselves stay in best-cosine-first order so a partial run
    still publishes the strongest work.

    Near-duplicate template pages propose A->B and B->A together and a bulk
    approval takes both, which writes a mutual link neither page needs. The
    queue's reciprocal filter only ever hid one of them from view; this is the
    gate that stops the second from being written. A suppressed row is returned
    rather than quietly dropped, because a row that can never publish and stays
    'approved' is counted as awaiting publication for ever.

    Only a reverse that is already *applied* is reported as superseded. The
    loser of an in-batch pair stays approved until its winner commits; a final
    pass in this same publication run then expires it. If the winner fails, the
    loser remains available and the deterministic arbitration repeats on retry.
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


def _expire_superseded(db, suggestion_ids: list[int]) -> int:
    if not suggestion_ids:
        return 0
    return db.execute(
        update(Suggestion)
        .where(Suggestion.id.in_(suggestion_ids), Suggestion.status == "approved")
        .values(status="expired")
        .execution_options(synchronize_session=False)
    ).rowcount


def generate_missing_placements(
    groups: list[tuple[int, list[int]]], job_run_id: int | None
) -> int:
    """Fill in placements for approved suggestions that never had a drawer opened.

    Generation is lazy by design: an analysis run produces far more suggestions
    than an editor ever opens, and paying per row was rejected. But the real
    workflow is bulk approval, so by publication time most approved rows have no
    anchor and publish as the appended block — the feature exists and almost
    never fires.

    Runs entirely before the write loop and outside any claim: a model call
    takes seconds, and the publication transaction holds an advisory lock plus a
    row lock for its whole duration. Suggestions are grouped by source article
    so each call can be told which anchors its siblings already took.
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
            # deterministic and the workers need no shared counter.
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
        # front of this same worker's writes.
        db.expunge_all()
        db.rollback()

        # One source article at a time within a group, because each call has to
        # know which anchors its siblings just claimed. Different articles share
        # nothing, so they overlap: the pass is latency-bound on an external API,
        # and 500 sequential calls at ~5s each would not fit the job timeout.
        with ThreadPoolExecutor(max_workers=settings.publish_placement_concurrency) as pool:
            results = list(pool.map(_generate_for_source, pending))

        generated = 0
        for source_results in results:
            for suggestion_id, placement in source_results:
                placement_service.store(db, suggestion_id, placement)
                generated += bool(placement.anchor_text)
        db.commit()
        if spent >= budget:
            logger.warning(
                "publication preflight stopped at %s placement calls; the rest publish "
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
    raised — an unreachable model is a reason to publish the appended block, not
    a reason to fail the publication run.
    """
    missing, taken = work
    results = []
    for suggestion in missing:
        try:
            placement = placement_service.generate(suggestion, taken_anchors=taken)
        except Exception:
            logger.exception(
                "preflight placement generation failed for suggestion %s", suggestion.id
            )
            continue
        results.append((suggestion.id, placement))
        if placement.anchor_text:
            taken.append(placement.anchor_text)
    return results


def publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, _publish_approved, site_id)


def _publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    db = SessionLocal()
    progress_db = SessionLocal() if job_run_id is not None else None
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)
        policy_expired = expire_ineligible_external_suggestions(
            db,
            site,
            statuses=("approved",),
            actor="system:publication-policy",
        )
        if policy_expired:
            db.commit()
            logger.info(
                "expired %s approved external suggestion(s) blocked by site %s policy",
                policy_expired,
                site_id,
            )
        groups, superseded = grouped_batch(db, site_id)
        if superseded:
            # Terminal, not skipped: this row can never publish, and left
            # approved it is reported as awaiting publication by every run for
            # ever while nothing about it changes.
            _expire_superseded(db, superseded)
            logger.info(
                "expired %s approved suggestion(s) on site %s already linked the other way",
                len(superseded),
                site_id,
            )
        batch_size = sum(len(ids) for _source_article_id, ids in groups)
        run = db.get(JobRun, job_run_id) if job_run_id is not None else None
        progress = begin_publication_attempt(
            run.progress if run is not None else None,
            batch_size,
        )
        failure_details: list[str] = []
        has_retryable_failure = False
        outcome_counts: dict[str, int] = defaultdict(
            int, resume_outcome_counts(run.progress if run is not None else None)
        )
        record_progress(db, job_run_id, **progress)
        db.commit()  # end the read transaction — each claim below gets its own

        generate_missing_placements(groups, job_run_id)
        if run is not None:
            # The preflight recorded its counters through its own session. This
            # one still holds the same JobRun unexpired, so without this the next
            # progress write merges onto a stale dict and drops them again.
            db.expire(run)

        for index, (source_article_id, suggestion_ids) in enumerate(groups):
            attempted = list(suggestion_ids)  # narrowed to the claim below
            charge_suggestions = False
            if index and settings.publish_request_delay_seconds:
                # Outside the claim on purpose: a pause taken while holding the
                # advisory lock would block every other worker on this article.
                sleep(settings.publish_request_delay_seconds)
            try:
                # one publication update per source article at a time
                db.execute(
                    select(func.pg_advisory_xact_lock(_PUBLISH_LOCK_NAMESPACE, source_article_id))
                ).scalar_one()
                db.execute(
                    update(Suggestion)
                    .where(
                        Suggestion.id.in_(suggestion_ids),
                        Suggestion.status == "approved",
                        Suggestion.source_article.has(Article.is_active.is_(True)),
                        or_(
                            Suggestion.target_article_id.is_(None),
                            Suggestion.target_article.has(Article.is_active.is_(True)),
                        ),
                    )
                    .values(status="applying")
                )
                claimed = db.scalars(
                    select(Suggestion)
                    .where(Suggestion.id.in_(suggestion_ids), Suggestion.status == "applying")
                    .options(
                        joinedload(Suggestion.source_article),
                        joinedload(Suggestion.target_article),
                    )
                ).all()
                by_id = {row.id: row for row in claimed}
                ordered = [by_id[sid] for sid in suggestion_ids if sid in by_id]
                # Skips and attempts are derived together, after both statements,
                # so every suggestion is counted exactly once: what was not
                # claimed is skipped, and only what was claimed can still fail.
                attempted = [row.id for row in ordered]
                for _ in range(len(suggestion_ids) - len(ordered)):
                    progress = record_publication_skip(progress)  # rejected, claimed, or inactive
                if not ordered:
                    db.rollback()
                    # The claim rollback also discards same-session progress. Reusing
                    # one independent session avoids connection churn; committing each
                    # skip keeps a killed all-skipped attempt fully accounted for.
                    record_progress_durably(job_run_id, session=progress_db, **progress)
                    continue

                try:
                    outcomes = connector.apply_links(ordered)
                except Exception:
                    # Only the customer's publication boundary consumes a
                    # suggestion attempt. Database and progress failures are
                    # infrastructure errors, not evidence that this row is bad.
                    charge_suggestions = True
                    raise
                applied_at = datetime.now(timezone.utc)
                committed = progress
                totals = dict(outcome_counts)
                for suggestion, outcome in zip(ordered, outcomes, strict=True):
                    # 'applied' is set exclusively here — lifecycle guarantee
                    suggestion.status = "applied"
                    suggestion.applied_at = applied_at
                    suggestion.publish_outcome = outcome
                    suggestion.publish_attempts = 0
                    suggestion.publish_error = None
                    totals[outcome] = totals.get(outcome, 0) + 1
                    committed = record_publication_applied(committed)
                record_progress(db, job_run_id, **committed, **totals)
                db.commit()  # releases the advisory and row locks
                # Adopted only once the commit that made them true has landed. A
                # failed commit puts the rows back to 'approved', and the except
                # branch below would then count the same suggestions as applied
                # *and* failed.
                progress = committed
                outcome_counts.update(totals)
            except Exception as error:
                db.rollback()  # claim undone — suggestions are 'approved' again for retry
                logger.exception("apply_links failed for source article %s", source_article_id)
                quarantined = (
                    _record_attempt_failure(
                        progress_db or db,
                        attempted,
                        f"{type(error).__name__}: {error}",
                    )
                    if charge_suggestions
                    else 0
                )
                if not charge_suggestions or quarantined < len(attempted):
                    has_retryable_failure = True
                for _ in attempted:
                    progress = record_publication_failure(progress)
                failure_details.append(
                    f"source article {source_article_id}: {type(error).__name__}: {error}"
                )
                if quarantined:
                    failure_details.append(
                        f"{quarantined} suggestion(s) quarantined after "
                        f"{settings.publish_max_suggestion_attempts} attempts"
                    )
                # This reusable session may have cached JobRun before a later
                # successful group committed new outcome totals elsewhere.
                record_progress_durably(
                    job_run_id,
                    session=progress_db,
                    **progress,
                    **dict(outcome_counts),
                )

        # A historical reciprocal loser becomes superseded only after its winner
        # commits. Retire it in this same run so /publish/pending drains now.
        _remaining, newly_superseded = grouped_batch(db, site_id)
        if _expire_superseded(db, newly_superseded):
            db.commit()
        if progress["attempt_failed"]:
            # Successful articles were committed individually and are skipped on
            # retry. Failed claims rolled back to "approved", so raising lets RQ
            # retry them and emit the exhausted-retry alert if they keep failing.
            details = "; ".join(failure_details[:3])
            error_type = RuntimeError if has_retryable_failure else NonRetryableTaskError
            raise error_type(
                f"{progress['attempt_failed']} publication suggestion(s) failed: {details}"
            )
        progress = complete_publication_success(progress)
        record_progress_durably(
            job_run_id, session=progress_db, **progress, **dict(outcome_counts)
        )
        return {
            "applied": progress["applied"],
            "failed": progress["failed"],
            "skipped": progress["skipped"],
            # What was actually written, not just that something was. The
            # in-text share is the number that says whether paying for placement
            # generation is worth it.
            "inserted": outcome_counts["inserted"],
            "block": outcome_counts["block"],
            "already_present": outcome_counts["already_present"],
            "policy_expired": policy_expired,
        }
    finally:
        db.close()
        if progress_db is not None:
            progress_db.close()


def _record_attempt_failure(db, suggestion_ids: list[int], reason: str) -> int:
    """Count this failure against each suggestion, quarantining the exhausted ones.

    Runs on its own session and commits, because the caller has just rolled back
    the claim: without a durable counter a permanently broken suggestion — a post
    locked by a plugin, a deleted post, a revoked application password — is
    retried by every publication run for ever, and nothing in the system ever
    says so.
    """
    limit = settings.publish_max_suggestion_attempts
    try:
        updated_ids = list(
            db.scalars(
                update(Suggestion)
                .where(Suggestion.id.in_(suggestion_ids), Suggestion.status == "approved")
                .values(
                    publish_attempts=Suggestion.publish_attempts + 1,
                    publish_error=reason[:2000],
                )
                .returning(Suggestion.id)
                .execution_options(synchronize_session=False)
            )
        )
        quarantined = db.execute(
            update(Suggestion)
            .where(
                Suggestion.id.in_(suggestion_ids),
                Suggestion.status == "approved",
                Suggestion.publish_attempts >= limit,
            )
            .values(status="failed")
            .execution_options(synchronize_session=False)
        ).rowcount
        attempts = db.execute(
            select(Suggestion.id, Suggestion.publish_attempts, Suggestion.status).where(
                Suggestion.id.in_(updated_ids)
            )
        ).all()
        db.add_all(
            SuggestionEvent(
                suggestion_id=suggestion_id,
                event_type="publish_attempt_failed",
                actor="system:publication",
                details={
                    "reason": reason[:2000],
                    "attempt": attempt_count,
                    "terminal": status == "failed",
                },
            )
            for suggestion_id, attempt_count, status in attempts
        )
        db.commit()
        return quarantined
    except Exception:
        db.rollback()
        logger.exception("could not record publication attempt for %s", suggestion_ids)
        return 0
