"""Publication worker: send approved artifacts, decide nothing.

This task used to choose the batch, arbitrate contested anchors, call a model for
missing placements, and render the HTML — all after the last human had left. An
operator approved an intention and this file invented the change. Every one of
those decisions now happens during preparation, in front of a person, and is
frozen in a `PublicationPlan` whose hash that person is bound to.

What remains here is deliberately small. For each approved plan: take the
per-source-article advisory lock, lock the plan and its suggestions, recompute
the hash and compare it with the one the operator approved, then hand two strings
to the connector. No OpenRouter, no placement generation, no `preview_links`, no
reciprocal arbitration, no rendering.

Four ways a plan ends:

- written / already_applied -> applied; each stored item outcome is copied to its
  suggestion, so "how often did a link land in the prose" stays answerable.
- the live article changed  -> stale. No write, no RQ retry (retrying would only
  re-read the same changed article), suggestions go back to being selected, and a
  durable alert says a human must prepare and approve a new plan.
- the artifact no longer hashes to its approved value -> failed, no write, no
  retry, durable alert. Only a person can say which version was meant.
- a transient connector error -> rolled back, still approved, RQ retries. A plan
  whose suggestions exhaust `publish_max_suggestion_attempts` becomes failed
  rather than being retried by every future run.

Publication never writes internal_links — the applied link is detected at the next
crawl (A9, ingestion is the single source of truth). Durable accounting spans all
attempts while retaining latest-attempt failure and skip diagnostics.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from time import sleep

from sqlalchemy import func, select, update

from app.config import settings
from app.connectors.base import StalePlanError
from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.models import Article, JobRun, PublicationPlan, Site, Suggestion
from app.services.alerts import send_alert
from app.services.publication_plan_service import (
    MAX_REASON_CHARS,
    PlanIntegrityError,
    load_approved_plans,
    mark_stale,
    verify_integrity,
)
from app.services.publication_progress import (
    begin_publication_attempt,
    complete_publication_success,
    record_publication_applied,
    record_publication_failure,
    record_publication_skip,
    resume_outcome_counts,
)
from app.services.job_service import (
    NonRetryableTaskError,
    record_progress,
    record_progress_durably,
    run_durably,
)

logger = logging.getLogger(__name__)

_PUBLISH_LOCK_NAMESPACE = 0x4C50  # "LP" - keyed by source article; see namespace registry


def publish_approved_plans(
    site_id: int,
    job_run_id: int | None = None,
    plan_ids: list[int] | None = None,
) -> dict:
    return run_durably(
        job_run_id,
        _publish_approved_plans,
        site_id,
        plan_ids=plan_ids,
    )


def _publish_approved_plans(
    site_id: int,
    job_run_id: int | None = None,
    plan_ids: list[int] | None = None,
) -> dict:
    db = SessionLocal()
    progress_db = SessionLocal() if job_run_id is not None else None
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)
        plans = load_approved_plans(db, site_id, plan_ids=plan_ids)
        # The batch is the links inside approved artifacts, not everything a
        # reviewer has selected. Those are different numbers now, and reporting
        # the larger one would describe work this run was never going to do.
        batch_size = sum(len(plan.items or []) for plan in plans)
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
        db.commit()  # end the read transaction — each plan below gets its own

        for index, plan in enumerate(plans):
            plan_id = plan.id
            source_article_id = plan.source_article_id
            item_count = len(plan.items or [])
            attempted: list[int] = []
            charge_suggestions = False
            if index and settings.publish_request_delay_seconds:
                # Outside the lock on purpose: a pause taken while holding the
                # advisory lock would block every other worker on this article.
                sleep(settings.publish_request_delay_seconds)
            try:
                # one publication update per source article at a time
                db.execute(
                    select(func.pg_advisory_xact_lock(_PUBLISH_LOCK_NAMESPACE, source_article_id))
                ).scalar_one()
                locked = db.scalars(
                    select(PublicationPlan)
                    .where(PublicationPlan.id == plan_id)
                    # `populate_existing` is the whole point of this re-read.
                    # Sessions here do not expire on commit, so without it the
                    # statement takes the row lock and then hands back the
                    # attributes loaded during the batch read — the worker would
                    # verify a snapshot from before the lock and act on a status
                    # another transaction has since changed.
                    .execution_options(populate_existing=True)
                    .with_for_update()
                ).first()
                if locked is None or locked.status != "approved":
                    # Another worker published, or an operator invalidated it,
                    # while this run was waiting on the lock.
                    db.rollback()
                    for _ in range(item_count):
                        progress = record_publication_skip(progress)
                    record_progress_durably(job_run_id, session=progress_db, **progress)
                    continue

                suggestions = db.scalars(
                    select(Suggestion)
                    .where(Suggestion.publication_plan_id == plan_id)
                    .order_by(Suggestion.id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                ).all()
                attempted = [row.id for row in suggestions]

                # Before any network access: the bytes about to be sent must
                # still be the bytes a named human approved.
                verify_integrity(locked)

                source = db.get(Article, source_article_id)
                if source is None:
                    raise PlanIntegrityError(
                        f"source article {source_article_id} of plan {plan_id} no longer exists"
                    )

                try:
                    outcome = connector.apply_planned_edit(
                        source,
                        original_html=locked.original_html,
                        updated_html=locked.updated_html,
                    )
                except StalePlanError as error:
                    db.rollback()
                    progress = _handle_stale_plan(
                        db, site_id, plan_id, str(error), item_count, progress
                    )
                    record_progress_durably(job_run_id, session=progress_db, **progress)
                    continue
                except Exception:
                    # Only the customer's publication boundary consumes a
                    # suggestion attempt. Database and progress failures are
                    # infrastructure errors, not evidence that this plan is bad.
                    charge_suggestions = True
                    raise

                applied_at = datetime.now(timezone.utc)
                committed = progress
                totals = dict(outcome_counts)
                by_id = {row.id: row for row in suggestions}
                for item in locked.items or []:
                    suggestion = by_id.get(item["suggestion_id"])
                    if suggestion is None:
                        # The link was written; the row that asked for it is gone.
                        # Count the outcome so the totals still describe the post.
                        totals[item["outcome"]] = totals.get(item["outcome"], 0) + 1
                        committed = record_publication_applied(committed)
                        continue
                    # 'applied' is set exclusively here — lifecycle guarantee
                    suggestion.status = "applied"
                    suggestion.applied_at = applied_at
                    # The outcome the operator was shown, not one re-derived now.
                    suggestion.publish_outcome = item["outcome"]
                    suggestion.publish_attempts = 0
                    suggestion.publish_error = None
                    totals[item["outcome"]] = totals.get(item["outcome"], 0) + 1
                    committed = record_publication_applied(committed)
                locked.status = "applied"
                locked.applied_at = applied_at
                record_progress(db, job_run_id, **committed, **totals)
                db.commit()  # releases the advisory and row locks
                # Adopted only once the commit that made them true has landed. A
                # failed commit puts the plan back to 'approved', and the except
                # branch below would then count the same links as applied *and*
                # failed.
                progress = committed
                outcome_counts.update(totals)
                if outcome == "already_applied" and item_count:
                    logger.info(
                        "plan %s was already written to WordPress; finalized without a second POST",
                        plan_id,
                    )
            except PlanIntegrityError as error:
                db.rollback()
                logger.error("publication plan %s failed integrity check: %s", plan_id, error)
                _fail_plan(db, site_id, plan_id, str(error), tampered=True)
                for _ in range(item_count):
                    progress = record_publication_failure(progress)
                failure_details.append(f"plan {plan_id}: {error}")
                record_progress_durably(
                    job_run_id, session=progress_db, **progress, **dict(outcome_counts)
                )
            except Exception as error:
                db.rollback()  # plan is 'approved' again for retry
                logger.exception("publication failed for plan %s", plan_id)
                quarantined = (
                    _record_attempt_failure(
                        progress_db or db,
                        attempted,
                        f"{type(error).__name__}: {error}",
                    )
                    if charge_suggestions
                    else 0
                )
                exhausted = bool(attempted) and quarantined >= len(attempted)
                if exhausted:
                    _fail_plan(
                        db,
                        site_id,
                        plan_id,
                        f"{type(error).__name__}: {error}",
                        tampered=False,
                    )
                if not exhausted:
                    has_retryable_failure = True
                for _ in range(item_count):
                    progress = record_publication_failure(progress)
                failure_details.append(f"plan {plan_id}: {type(error).__name__}: {error}")
                if quarantined:
                    failure_details.append(
                        f"{quarantined} suggestion(s) quarantined after "
                        f"{settings.publish_max_suggestion_attempts} attempts"
                    )
                # This reusable session may have cached JobRun before a later
                # successful plan committed new outcome totals elsewhere.
                record_progress_durably(
                    job_run_id,
                    session=progress_db,
                    **progress,
                    **dict(outcome_counts),
                )

        if progress["attempt_failed"]:
            # Successful plans were committed individually and are skipped on
            # retry. Failed plans rolled back to "approved", so raising lets RQ
            # retry them and emit the exhausted-retry alert if they keep failing.
            details = "; ".join(failure_details[:3])
            error_type = RuntimeError if has_retryable_failure else NonRetryableTaskError
            raise error_type(f"{progress['attempt_failed']} publication link(s) failed: {details}")
        progress = complete_publication_success(progress)
        record_progress_durably(job_run_id, session=progress_db, **progress, **dict(outcome_counts))
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
        }
    finally:
        db.close()
        if progress_db is not None:
            progress_db.close()


def _handle_stale_plan(
    db,
    site_id: int,
    plan_id: int,
    reason: str,
    item_count: int,
    progress: dict,
) -> dict:
    """The article changed after approval: refuse, explain, and stop asking.

    Not a failure of this run and not a retry: RQ would only re-read the same
    changed article. The suggestions were good editorial decisions and stay
    selected; what is gone is the rendering, so a human prepares and approves a
    new one. Counted as skipped, which is exactly what happened to those links.
    """
    plan = db.get(PublicationPlan, plan_id)
    if plan is not None and plan.status == "approved":
        mark_stale(db, plan, reason)
    send_alert(
        "LinkMesh publication plan is stale",
        {
            "site_id": site_id,
            "plan_id": plan_id,
            "reason": reason[:MAX_REASON_CHARS],
            "action": "prepare and approve a new plan for this article",
        },
        kind="publication_plan_stale",
        site_id=site_id,
    )
    for _ in range(item_count):
        progress = record_publication_skip(progress)
    return progress


def _fail_plan(db, site_id: int, plan_id: int, reason: str, *, tampered: bool) -> None:
    """Retire a plan that must not be retried, keeping its suggestion links.

    The links stay for the audit trail: an integrity failure is the one case
    where the question "what did this artifact claim, and which rows fed it" has
    to remain answerable. Recovery is an explicit reselection by a person, which
    is what clears the link.
    """
    try:
        plan = db.get(PublicationPlan, plan_id)
        if plan is not None and plan.status == "approved":
            plan.status = "failed"
            plan.invalidated_at = datetime.now(timezone.utc)
            plan.failure_reason = reason[:MAX_REASON_CHARS]
            db.execute(
                update(Suggestion)
                .where(Suggestion.publication_plan_id == plan_id)
                .values(status="failed", publish_error=reason[:2000])
                .execution_options(synchronize_session=False)
            )
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("could not retire publication plan %s", plan_id)
    send_alert(
        (
            "LinkMesh publication plan failed integrity check"
            if tampered
            else "LinkMesh publication plan failed"
        ),
        {
            "site_id": site_id,
            "plan_id": plan_id,
            "reason": reason[:MAX_REASON_CHARS],
            "action": "review the article and select its suggestions again",
        },
        kind="publication_plan_failed",
        site_id=site_id,
    )


def _record_attempt_failure(db, suggestion_ids: list[int], reason: str) -> int:
    """Count this failure against each suggestion, quarantining the exhausted ones.

    Runs on its own session and commits, because the caller has just rolled back
    the plan: without a durable counter a permanently broken article — a post
    locked by a plugin, a deleted post, a revoked application password — is
    retried by every publication run for ever, and nothing in the system ever
    says so.
    """
    if not suggestion_ids:
        return 0
    limit = settings.publish_max_suggestion_attempts
    try:
        db.execute(
            update(Suggestion)
            .where(Suggestion.id.in_(suggestion_ids), Suggestion.status == "approved")
            .values(publish_attempts=Suggestion.publish_attempts + 1, publish_error=reason[:2000])
            .execution_options(synchronize_session=False)
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
        db.commit()
        return quarantined
    except Exception:
        db.rollback()
        logger.exception("could not record publication attempt for %s", suggestion_ids)
        return 0
