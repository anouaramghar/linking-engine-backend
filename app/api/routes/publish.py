"""Publication is three explicit steps, and only the middle one is an approval.

    POST /publish/{id}/plans/prepare-async  queue the live read, render and store
    POST /publish/{id}/plans/approve        a named human binds themselves to hashes
    POST /publish/{id}                      queue the approved artifacts, nothing else

Preparation may spend money and read the managed site; it writes nothing back.
It is only ever queued: the work is one live WordPress request per source
article plus a placement model call, which is far too slow and too expensive to
hold an API worker, and its owner has to be a named operator rather than
whichever key opened the connection. Approval is the only place a human decision
is recorded. Queueing makes no decision at all, which is what makes it safe to
retry after a failure.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key, require_operator_identity, require_site_access
from app.models import PublicationPlan, Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.publication import (
    PendingPublicationPage,
    PendingPublicationSite,
    PlanApprovalRequest,
    PlanApprovalResult,
    PublicationQueueRequest,
    PublicationPlanHtml,
)
from app.services import publication_plan_service
from app.services.authorization import Principal, tenant_site_filter
from app.services.job_service import DuplicateJobError, enqueue_job
from app.services.publication_plan_service import PlanApprovalError
from app.tasks.publication import (
    prepare_publication_plans as prepare_publication_plans_job,
    publish_approved_plans,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/publish", tags=["publish"])


def _pending_publication_query(principal: Principal, search: str | None = None):
    selected = (
        select(Suggestion.site_id, func.count().label("selected_suggestions"))
        .where(
            Suggestion.status == "approved",
            Suggestion.publication_plan_id.is_(None),
        )
        .group_by(Suggestion.site_id)
        .subquery()
    )
    approved = (
        select(PublicationPlan.site_id, func.count().label("approved_plans"))
        .where(PublicationPlan.status == "approved")
        .group_by(PublicationPlan.site_id)
        .subquery()
    )
    selected_count = func.coalesce(selected.c.selected_suggestions, 0)
    approved_count = func.coalesce(approved.c.approved_plans, 0)
    query = (
        select(
            Site.id.label("site_id"),
            Site.name.label("site_name"),
            Site.platform,
            selected_count.label("selected_suggestions"),
            approved_count.label("approved_plans"),
            and_(
                Site.platform == "wordpress",
                Site.wp_username.is_not(None),
                Site.wp_username != "",
            ).label("can_publish"),
        )
        .outerjoin(selected, selected.c.site_id == Site.id)
        .outerjoin(approved, approved.c.site_id == Site.id)
        .where(or_(selected_count > 0, approved_count > 0))
    )
    owned = tenant_site_filter(principal)
    if owned is not None:
        query = query.where(owned)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(or_(Site.name.ilike(pattern), Site.base_url.ilike(pattern)))
    return query


@router.get("/pending", response_model=PendingPublicationPage)
def pending_publication_sites(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None, min_length=1, max_length=255),
    include_totals: bool = Query(default=True),
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PendingPublicationPage:
    """Per site: work awaiting preparation, and artifacts awaiting a queue.

    Two independent counts rather than one "awaiting publication", because they
    ask for different actions. Selected suggestions need someone to prepare and
    approve them; approved plans need only a job.
    """
    inbox = _pending_publication_query(principal, search).subquery()
    totals = (
        db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(inbox.c.selected_suggestions), 0),
                func.coalesce(func.sum(inbox.c.approved_plans), 0),
            ).select_from(inbox)
        ).one()
        if include_totals
        else (None, None, None)
    )
    page = select(inbox)
    if cursor is not None:
        page = page.where(inbox.c.site_id > cursor)
    rows = db.execute(page.order_by(inbox.c.site_id).limit(limit + 1)).mappings().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return PendingPublicationPage(
        items=[PendingPublicationSite.model_validate(dict(row)) for row in rows],
        next_cursor=rows[-1]["site_id"] if has_more and rows else None,
        total_sites=totals[0],
        total_selected_suggestions=totals[1],
        total_approved_plans=totals[2],
    )


@router.get("/pending/{site_id}", response_model=PendingPublicationSite)
def pending_publication_site(
    site_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PendingPublicationSite:
    row = (
        db.execute(_pending_publication_query(principal).where(Site.id == site_id))
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(404, "this site has no publication work waiting")
    return PendingPublicationSite.model_validate(dict(row))


@router.post(
    "/{site_id}/plans/prepare-async",
    status_code=202,
    response_model=JobAccepted,
)
def prepare_publication_plans_async(
    site: Site = Depends(require_site_access),
    operator_id: str = Depends(require_operator_identity),
    max_articles: int = Query(default=10, ge=1, le=100),
    suggestion_ids: list[int] | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> JobAccepted:
    """Queue live preparation so slow WordPress reads never occupy an API worker.

    This is the only way to prepare. Every decision publication used to make on
    its own — cohort, order, anchor arbitration, in-text or appended block, the
    rendered HTML — is made by the worker and stored. What the job returns is
    not a preview of a future decision; it *is* the decision, and approving its
    hash is what allows it to be sent.

    `suggestion_ids` prepares only those links. A plan's hash covers a whole
    source article, so approving one link out of an article holding several is
    possible only if the artifact was rendered for that link alone — which is a
    decision, and therefore has to be made here rather than by hiding rows in a
    dashboard. Ids belonging to another site match nothing and yield an empty
    preparation; the cohort query is scoped to this site and says nothing about
    them either way.

    An id that names a link on this site which is *not* selected, is already
    bound to a plan, or lost a reciprocal pair, is likewise absent from the
    result rather than rejected: it is the same "not in this batch" answer the
    operator gets when the article could not be read.
    """
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources are read-only")
    # Checked once here rather than discovered per article. Preparation reads
    # every source post with `context=edit`, which WordPress refuses without an
    # account, so an unauthenticated site produced one live request and one
    # identical 401 per article before showing the operator an empty batch.
    if not site.has_wordpress_credentials:
        raise HTTPException(
            409,
            "this site has no WordPress account, so its posts cannot be read for editing "
            "or written to; add an application password for a user who can edit posts",
        )
    try:
        run = enqueue_job(
            db,
            site.id,
            "publication_preparation",
            prepare_publication_plans_job,
            job_timeout=900,
            task_kwargs={"max_articles": max_articles, "suggestion_ids": suggestion_ids},
            requested_by=operator_id,
        )
    except DuplicateJobError as error:
        if error.run.requested_by == operator_id and error.run.queue_job_id:
            # One preparation per site at a time, so this operator joins the one
            # already running rather than reading the same posts twice. It may
            # have a different scope from the one just asked for — the dashboard
            # shows the named link out of whatever batch comes back, and a wider
            # result is never approved by a narrower review.
            run = error.run
        else:
            raise HTTPException(
                409, "this site is already being prepared by another operator"
            ) from error
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.get(
    "/{site_id}/plans/{plan_id}/html",
    response_model=PublicationPlanHtml,
)
def publication_plan_html(
    plan_id: int,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> PublicationPlanHtml:
    plan = db.scalar(
        select(PublicationPlan).where(
            PublicationPlan.id == plan_id,
            PublicationPlan.site_id == site.id,
        )
    )
    if plan is None:
        raise HTTPException(404, "publication plan not found")
    return PublicationPlanHtml(
        id=plan.id,
        plan_hash=plan.plan_hash,
        original_html=plan.original_html,
        updated_html=plan.updated_html,
    )


@router.post("/{site_id}/plans/approve", response_model=PlanApprovalResult)
def approve_publication_plans(
    payload: PlanApprovalRequest,
    site: Site = Depends(require_site_access),
    operator_id: str = Depends(require_operator_identity),
    db: Session = Depends(get_db),
) -> PlanApprovalResult:
    """The one human decision in the whole pipeline, recorded against a person.

    A shared service key cannot make it: `require_operator_identity` demands a
    dashboard session or an operator-specific key, because "approved_by:
    linkmesh-api" is not an audit trail.

    All or nothing. If any named plan has changed, moved on, or lost a
    suggestion, nothing is approved and the operator reloads — an approval of
    "whichever ones still match" is an approval of a set nobody read.
    """
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources are read-only")
    try:
        plans = publication_plan_service.approve_plans(
            db,
            site.id,
            [(plan.id, plan.plan_hash) for plan in payload.plans],
            approved_by=operator_id,
        )
    except PlanApprovalError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from error
    return PlanApprovalResult(approved=[plan.id for plan in plans], approved_by=operator_id)


@router.post("/{site_id}", status_code=202, response_model=JobAccepted)
def trigger_publication(
    payload: PublicationQueueRequest | None = None,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> JobAccepted:
    """Queue the approved artifacts. This endpoint decides nothing.

    It cannot promote a selected suggestion into a publishable change, which is
    what the old version of this route did every time it was called. With no
    approved plan there is nothing to queue, and saying so is a 409 rather than
    an empty job that reports success.
    """
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources are read-only")
    approved_query = select(PublicationPlan.id).where(
        PublicationPlan.site_id == site.id,
        PublicationPlan.status == "approved",
    )
    if payload is not None:
        approved_query = approved_query.where(PublicationPlan.id.in_(payload.plan_ids))
    approved_ids = list(db.scalars(approved_query))
    if payload is not None and len(approved_ids) != len(payload.plan_ids):
        raise HTTPException(
            409,
            "one or more requested publication plans are no longer approved for this site",
        )
    if not approved_ids:
        raise HTTPException(
            409,
            "no approved publication plan for this site; prepare the edits and "
            "approve them before publishing",
        )
    try:
        run = enqueue_job(
            db,
            site.id,
            "publication",
            publish_approved_plans,
            job_timeout=3600,
            task_kwargs={"plan_ids": payload.plan_ids} if payload is not None else None,
        )
    except DuplicateJobError as e:
        raise HTTPException(409, str(e)) from e
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.get("/{site_id}/status")
def publication_status(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(Suggestion.status, func.count())
        .where(
            Suggestion.site_id == site.id,
            or_(
                Suggestion.status == "applied",
                and_(
                    Suggestion.status == "approved",
                    Suggestion.publication_plan_id.is_(None),
                ),
            ),
        )
        .group_by(Suggestion.status)
    ).all()
    counts = dict(rows)
    approved_plans = (
        db.scalar(
            select(func.count())
            .select_from(PublicationPlan)
            .where(PublicationPlan.site_id == site.id, PublicationPlan.status == "approved")
        )
        or 0
    )
    return {
        "applied": counts.get("applied", 0),
        # Selected, not approved: these rows are editorial intent, and none of
        # them is publishable until it is part of an approved plan.
        "selected_suggestions": counts.get("approved", 0),
        "approved_plans": approved_plans,
    }
