"""Publication is three explicit steps, and only the middle one is an approval.

    POST /publish/{id}/plans/prepare   read the live posts, render, store, show
    POST /publish/{id}/plans/approve   a named human binds themselves to hashes
    POST /publish/{id}                 queue the approved artifacts, nothing else

Preparation may spend money and read a customer's site; it writes nothing back.
Approval is the only place a human decision is recorded. Queueing makes no
decision at all, which is what makes it safe to retry after a failure.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key, require_operator_identity, require_site_access
from app.models import PublicationPlan, Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.publication import (
    PendingPublicationSite,
    PlanApprovalRequest,
    PlanApprovalResult,
    PlanLink,
    PublicationQueueRequest,
    PublicationPlanOut,
    PublicationPreparationError,
    PublicationPreparationOut,
)
from app.services import publication_plan_service
from app.services.authorization import Principal, tenant_site_filter
from app.services.job_service import DuplicateJobError, enqueue_job
from app.services.publication_plan_service import PlanApprovalError
from app.tasks.publication import publish_approved_plans

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/publish", tags=["publish"])


def _plan_out(plan: PublicationPlan) -> PublicationPlanOut:
    return PublicationPlanOut(
        id=plan.id,
        status=plan.status,
        plan_hash=plan.plan_hash,
        source_article_id=plan.source_article_id,
        source_url=plan.source_url,
        original_html=plan.original_html,
        updated_html=plan.updated_html,
        links=[PlanLink(**item) for item in (plan.items or [])],
    )


@router.get("/pending", response_model=list[PendingPublicationSite])
def pending_publication_sites(
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> list[PendingPublicationSite]:
    """Per site: work awaiting preparation, and artifacts awaiting a queue.

    Two independent counts rather than one "awaiting publication", because they
    ask for different actions. Selected suggestions need someone to prepare and
    approve them; approved plans need only a job.
    """
    owned = tenant_site_filter(principal)

    selected_query = (
        select(Suggestion.site_id, func.count().label("selected"))
        .where(
            Suggestion.status == "approved",
            Suggestion.publication_plan_id.is_(None),
        )
        .group_by(Suggestion.site_id)
    )
    plans_query = (
        select(PublicationPlan.site_id, func.count().label("approved_plans"))
        .where(PublicationPlan.status == "approved")
        .group_by(PublicationPlan.site_id)
    )
    if owned is not None:
        selected_query = selected_query.join(Site, Site.id == Suggestion.site_id).where(owned)
        plans_query = plans_query.join(Site, Site.id == PublicationPlan.site_id).where(owned)

    selected = dict(db.execute(selected_query).all())
    approved = dict(db.execute(plans_query).all())
    site_ids = sorted(set(selected) | set(approved))
    # One query for the sites behind those counts, so the dashboard can say that
    # a site cannot publish before anyone opens a review for it.
    sites = db.scalars(select(Site).where(Site.id.in_(site_ids))).all() if site_ids else []
    publishable = {site.id: site.has_wordpress_credentials for site in sites}
    return [
        PendingPublicationSite(
            site_id=site_id,
            selected_suggestions=selected.get(site_id, 0),
            approved_plans=approved.get(site_id, 0),
            can_publish=publishable.get(site_id, False),
        )
        for site_id in site_ids
    ]


@router.post("/{site_id}/plans/prepare", response_model=PublicationPreparationOut)
def prepare_publication_plans(
    site: Site = Depends(require_site_access),
    # Each source article is a live request to the customer's site, so a
    # synchronous preparation is bounded rather than covering the whole queue.
    max_articles: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PublicationPreparationOut:
    """Freeze what publication would write, decided against the live posts.

    Every decision publication used to make on its own — cohort, order, anchor
    arbitration, in-text or appended block, the rendered HTML — is made here and
    stored. What comes back is not a preview of a future decision; it *is* the
    decision, and approving its hash is what allows it to be sent.
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

    preparation = publication_plan_service.prepare_site(db, site, max_articles=max_articles)
    return PublicationPreparationOut(
        site_id=preparation.site_id,
        selected_suggestions=preparation.selected_suggestions,
        plans=[_plan_out(plan) for plan in preparation.plans],
        errors=[
            PublicationPreparationError(
                source_article_id=error.source_article_id,
                source_url=error.source_url,
                message=error.message,
            )
            for error in preparation.errors
        ],
        has_more=preparation.has_more,
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
