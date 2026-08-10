import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, require_api_key, require_site_access
from app.connectors.registry import get_connector
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.publication import (
    PendingPublicationSite,
    PlannedArticle,
    PlannedLink,
    PublicationDryRun,
    PublicationPreviewError,
)
from app.services.authorization import Principal, tenant_site_filter
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.publication import generate_missing_placements, grouped_batch, publish_approved

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/publish", tags=["publish"])


@router.get("/pending", response_model=list[PendingPublicationSite])
def pending_publication_sites(
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> list[PendingPublicationSite]:
    query = (
        select(
            Suggestion.site_id,
            func.count().label("awaiting_publication"),
        )
        .where(Suggestion.status == "approved")
        .group_by(Suggestion.site_id)
        .order_by(Suggestion.site_id)
    )
    owned = tenant_site_filter(principal)
    if owned is not None:
        query = query.join(Site, Site.id == Suggestion.site_id).where(owned)
    rows = db.execute(query).all()
    return [
        PendingPublicationSite(
            site_id=site_id,
            awaiting_publication=awaiting_publication,
        )
        for site_id, awaiting_publication in rows
    ]


@router.post("/{site_id}", status_code=202, response_model=JobAccepted)
def trigger_publication(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> JobAccepted:
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources are read-only")
    try:
        run = enqueue_job(db, site.id, "publication", publish_approved, job_timeout=3600)
    except DuplicateJobError as e:
        raise HTTPException(409, str(e)) from e
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.post("/{site_id}/dry-run", response_model=PublicationDryRun)
def dry_run_publication(
    site: Site = Depends(require_site_access),
    # Each source article is a live request to the customer's site, so a
    # synchronous preview is bounded rather than covering the whole queue.
    max_articles: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PublicationDryRun:
    """What publication would write, decided against the live posts, without writing.

    There was no way to see this before it happened. The same reads, the same
    ordering and the same in-text-or-block decisions as a real run — but no
    WordPress POST and no claim. Missing placements are prepared first and
    stored internally, so the subsequent publish reuses the choices shown here.
    """
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources are read-only")

    # A preview writes nothing, so the superseded rows are left for the run
    # itself to expire.
    groups, _superseded = grouped_batch(db, site.id)
    approved = (
        db.scalar(
            select(func.count())
            .select_from(Suggestion)
            .where(Suggestion.site_id == site.id, Suggestion.status == "approved")
        )
        or 0
    )
    preview_groups = groups[:max_articles]
    generate_missing_placements(preview_groups, None)
    connector = get_connector(site)
    planned: list[PlannedLink] = []
    articles: list[PlannedArticle] = []
    errors: list[PublicationPreviewError] = []
    placements_missing = 0
    for _source_article_id, suggestion_ids in preview_groups:
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
            errors.append(
                PublicationPreviewError(
                    source_article_id=_source_article_id,
                    source_url="",
                    message="approved suggestions changed while the preview was loading",
                )
            )
            continue
        placements_missing += sum(1 for row in ordered if row.placement_generated_at is None)
        try:
            preview = connector.preview_links(ordered)
        except Exception as error:
            # One unreachable post must not lose the preview of the others.
            logger.warning("dry run failed for source article %s: %s", _source_article_id, error)
            source = ordered[0].source_article if ordered else None
            errors.append(
                PublicationPreviewError(
                    source_article_id=_source_article_id,
                    source_url=source.url if source is not None else "",
                    message=str(error)[:500],
                )
            )
            continue
        links = [
            PlannedLink(
                suggestion_id=suggestion.id,
                source_article_id=suggestion.source_article_id,
                source_url=suggestion.source_article.url,
                target_url=suggestion.target_article.url,
                outcome=outcome,
                anchor_text=suggestion.anchor_text if outcome == "inserted" else None,
            )
            for suggestion, outcome in zip(ordered, preview.outcomes, strict=True)
        ]
        planned.extend(links)
        articles.append(
            PlannedArticle(
                source_article_id=_source_article_id,
                source_url=ordered[0].source_article.url,
                original_html=preview.original_content,
                updated_html=preview.updated_content,
                links=links,
            )
        )
    return PublicationDryRun(
        site_id=site.id,
        approved=approved,
        previewed=len(planned),
        placements_missing=placements_missing,
        inserted=sum(1 for link in planned if link.outcome == "inserted"),
        block=sum(1 for link in planned if link.outcome == "block"),
        already_present=sum(1 for link in planned if link.outcome == "already_present"),
        planned=planned,
        articles=articles,
        errors=errors,
        truncated=len(groups) > max_articles,
    )


@router.get("/{site_id}/status")
def publication_status(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(Suggestion.status, func.count())
        .where(Suggestion.site_id == site.id, Suggestion.status.in_(["approved", "applied"]))
        .group_by(Suggestion.status)
    ).all()
    counts = dict(rows)
    return {"applied": counts.get("applied", 0), "awaiting_publication": counts.get("approved", 0)}
