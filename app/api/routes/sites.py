from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_operator_identity
from app.api.pagination import MAX_PAGE_SIZE
from app.config import settings
from app.models import (
    Article,
    IngestionRun,
    InternalLink,
    JobRun,
    PoolSourceAuditEvent,
    Site,
    Suggestion,
)
from app.schemas.pool_audit import PoolSourceAuditEventOut
from app.schemas.site import (
    ArticleOut,
    SiteBulkCreated,
    SiteBulkFailure,
    SiteBulkRequest,
    SiteBulkResult,
    SiteCreate,
    SiteOut,
    SiteSuggestionModeState,
    SiteSuggestionModeUpdate,
)
from app.services.ingestion_service import latest_run
from app.services.pool_source_policy import (
    PoolSourcePolicyError,
    expire_pool_target_suggestions,
    require_allowed_pool_domain,
)
from app.services.pool_source_audit import record_pool_source_audit_event

router = APIRouter(prefix="/sites", tags=["sites"])

DUPLICATE_REASON = "a site with this base_url already exists"


def _get_site_or_404(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, f"site {site_id} not found")
    return site


def _first_error(exc: ValidationError) -> str:
    """Flatten a row's validation failure into one reviewer-readable line."""
    error = exc.errors()[0]
    message = error["msg"].removeprefix("Value error, ")
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {message}" if location else message


def _site_counts(
    db: Session,
    site_ids: list[int],
) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
    if not site_ids:
        return {}, {}, {}

    article_counts = dict(
        db.execute(
            select(Article.site_id, func.count(Article.id))
            .where(
                Article.site_id.in_(site_ids),
                Article.is_active.is_(True),
            )
            .group_by(Article.site_id)
        ).all()
    )
    internal_link_counts = dict(
        db.execute(
            select(Article.site_id, func.count(InternalLink.id))
            .join(InternalLink, InternalLink.source_article_id == Article.id)
            .where(
                Article.site_id.in_(site_ids),
                Article.is_active.is_(True),
                InternalLink.is_active.is_(True),
            )
            .group_by(Article.site_id)
        ).all()
    )
    active_suggestion_counts = dict(
        db.execute(
            select(Suggestion.site_id, func.count(Suggestion.id))
            .join(Article, Article.id == Suggestion.source_article_id)
            .where(
                Suggestion.site_id.in_(site_ids),
                Article.is_active.is_(True),
                Suggestion.status.in_(("pending", "approved", "applying")),
            )
            .group_by(Suggestion.site_id)
        ).all()
    )
    return article_counts, internal_link_counts, active_suggestion_counts


def _latest_analyses(db: Session, site_ids: list[int]) -> dict[int, JobRun]:
    """The last *finished* analysis run per site.

    A crawl and an analysis are separate jobs, so the crawl run alone cannot say
    whether a site has suggestions yet. In-flight analyses already reach the UI
    through the active-jobs feed, so a resting row only needs the last outcome.
    """
    if not site_ids:
        return {}

    newest = (
        select(func.max(JobRun.id))
        .where(
            JobRun.site_id.in_(site_ids),
            JobRun.kind == "analysis",
            JobRun.status.in_(("succeeded", "failed")),
        )
        .group_by(JobRun.site_id)
        .scalar_subquery()
    )
    runs = db.scalars(select(JobRun).where(JobRun.id.in_(newest))).all()
    return {run.site_id: run for run in runs}


def _suggestion_mode_state(_site: Site) -> SiteSuggestionModeState:
    return SiteSuggestionModeState(
        suggestion_mode="experimental",
        suggestion_mode_managed=True,
        suggestion_comparison_enabled=False,
    )


def _site_out(
    site: Site,
    *,
    article_count: int,
    internal_link_count: int,
    active_suggestion_count: int,
    run: IngestionRun | None,
    analysis: JobRun | None = None,
) -> SiteOut:
    item = SiteOut.model_validate(site)
    mode = _suggestion_mode_state(site)
    item.suggestion_mode = mode.suggestion_mode
    item.suggestion_mode_managed = mode.suggestion_mode_managed
    item.suggestion_comparison_enabled = mode.suggestion_comparison_enabled
    site_capacity = min(
        article_count * settings.hybrid_max_suggestions_per_article,
        settings.hybrid_max_active_suggestions_per_site,
    )
    item.suggestion_slots_available = max(0, site_capacity - active_suggestion_count)
    if site.platform == "pool":
        item.suggestion_slots_available = 0
    item.article_count = article_count
    item.internal_link_count = internal_link_count
    if run is not None:
        item.last_ingestion_status = run.status
        item.last_crawl_at = run.finished_at or run.started_at
    if analysis is not None:
        item.last_analysis_status = analysis.status
        item.last_analysis_at = analysis.finished_at or analysis.enqueued_at
    return item


@router.post("", status_code=201, response_model=SiteOut)
def create_site(payload: SiteCreate, db: Session = Depends(get_db)) -> Site:
    if db.scalar(select(Site).where(Site.base_url == payload.base_url)):
        raise HTTPException(409, DUPLICATE_REASON)
    site = Site(**payload.model_dump())
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


@router.post("/bulk", response_model=SiteBulkResult)
def bulk_create_sites(payload: SiteBulkRequest, db: Session = Depends(get_db)) -> SiteBulkResult:
    """Create many sites in one request, reporting the outcome of every row.

    Partial success is the contract: a row that fails validation or collides with an
    existing site is reported and skipped, and the rest of the upload still lands. Each
    insert runs in its own savepoint so one collision cannot poison the batch.
    """
    created: list[SiteBulkCreated] = []
    skipped: list[SiteBulkFailure] = []
    rejected: list[SiteBulkFailure] = []
    seen: set[str] = set()

    for index, row in enumerate(payload.sites, start=1):
        try:
            item = SiteCreate.model_validate(row.model_dump())
        except ValidationError as exc:
            rejected.append(
                SiteBulkFailure(row=index, base_url=row.base_url, reason=_first_error(exc))
            )
            continue

        # `item.base_url` is normalized by SiteCreate, so both checks compare like for like.
        if item.base_url in seen:
            skipped.append(
                SiteBulkFailure(
                    row=index,
                    base_url=item.base_url,
                    reason="duplicate base_url within this upload",
                )
            )
            continue
        seen.add(item.base_url)

        if db.scalar(select(Site.id).where(Site.base_url == item.base_url)):
            skipped.append(
                SiteBulkFailure(row=index, base_url=item.base_url, reason=DUPLICATE_REASON)
            )
            continue

        site = Site(**item.model_dump())
        try:
            with db.begin_nested():
                db.add(site)
                db.flush()
        except IntegrityError:  # a concurrent import claimed the same base_url
            skipped.append(
                SiteBulkFailure(row=index, base_url=item.base_url, reason=DUPLICATE_REASON)
            )
            continue

        created.append(
            SiteBulkCreated(row=index, id=site.id, name=site.name, base_url=site.base_url)
        )

    db.commit()
    return SiteBulkResult(created=created, skipped=skipped, rejected=rejected)


@router.get("", response_model=list[SiteOut])
def list_sites(
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[SiteOut]:
    sites = db.scalars(select(Site).order_by(Site.id).limit(limit).offset(offset)).all()
    article_counts, internal_link_counts, active_suggestion_counts = _site_counts(
        db, [site.id for site in sites]
    )
    analyses = _latest_analyses(db, [site.id for site in sites])
    out = []
    for site in sites:
        run = latest_run(db, site.id)
        out.append(
            _site_out(
                site,
                article_count=article_counts.get(site.id, 0),
                internal_link_count=internal_link_counts.get(site.id, 0),
                active_suggestion_count=active_suggestion_counts.get(site.id, 0),
                run=run,
                analysis=analyses.get(site.id),
            )
        )
    return out


@router.get(
    "/{site_id}/pool-source/audit-events",
    response_model=list[PoolSourceAuditEventOut],
)
def list_pool_source_audit_events(
    site_id: int,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[PoolSourceAuditEvent]:
    return db.scalars(
        select(PoolSourceAuditEvent)
        .where(PoolSourceAuditEvent.site_id == site_id)
        .order_by(PoolSourceAuditEvent.created_at.desc(), PoolSourceAuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()


@router.get("/{site_id}", response_model=SiteOut)
def get_site(site_id: int, db: Session = Depends(get_db)) -> SiteOut:
    site = _get_site_or_404(db, site_id)
    article_counts, internal_link_counts, active_suggestion_counts = _site_counts(db, [site.id])
    run = latest_run(db, site.id)
    return _site_out(
        site,
        article_count=article_counts.get(site.id, 0),
        internal_link_count=internal_link_counts.get(site.id, 0),
        active_suggestion_count=active_suggestion_counts.get(site.id, 0),
        run=run,
        analysis=_latest_analyses(db, [site.id]).get(site.id),
    )


@router.post("/{site_id}/pool-source/approval", response_model=SiteOut)
def approve_pool_source(
    site_id: int,
    db: Session = Depends(get_db),
    operator_id: str = Depends(require_operator_identity),
) -> SiteOut:
    site = _get_site_or_404(db, site_id)
    if site.platform != "pool":
        raise HTTPException(409, f"site {site_id} is not a content-pool source")
    try:
        require_allowed_pool_domain(site.base_url)
    except PoolSourcePolicyError as error:
        raise HTTPException(409, str(error)) from error
    site.pool_source_approved = True
    site.pool_source_approved_at = datetime.now(UTC)
    site.pool_source_approved_by = operator_id
    record_pool_source_audit_event(db, site, "approved", operator_id)
    db.commit()
    return get_site(site_id, db)


@router.delete("/{site_id}/pool-source/approval", response_model=SiteOut)
def revoke_pool_source_approval(
    site_id: int,
    db: Session = Depends(get_db),
    operator_id: str = Depends(require_operator_identity),
) -> SiteOut:
    site = _get_site_or_404(db, site_id)
    if site.platform != "pool":
        raise HTTPException(409, f"site {site_id} is not a content-pool source")
    site.pool_source_approved = False
    site.pool_source_approved_at = None
    site.pool_source_approved_by = None
    expire_pool_target_suggestions(db, site.id, reason="revoked")
    record_pool_source_audit_event(db, site, "revoked", operator_id)
    db.commit()
    return get_site(site_id, db)


@router.post("/{site_id}/pool-source/reactivate", response_model=SiteOut)
def reactivate_pool_source(
    site_id: int,
    db: Session = Depends(get_db),
    operator_id: str = Depends(require_operator_identity),
) -> SiteOut:
    site = _get_site_or_404(db, site_id)
    if site.platform != "pool":
        raise HTTPException(409, f"site {site_id} is not a content-pool source")
    if not site.pool_source_approved:
        raise HTTPException(409, f"pool source site {site_id} must be approved first")
    try:
        require_allowed_pool_domain(site.base_url)
    except PoolSourcePolicyError as error:
        raise HTTPException(409, str(error)) from error
    site.pool_source_consecutive_failures = 0
    site.pool_source_quarantined = False
    site.pool_source_quarantined_at = None
    site.pool_source_quarantine_reason = None
    site.pool_source_last_reactivated_at = datetime.now(UTC)
    site.pool_source_last_reactivated_by = operator_id
    record_pool_source_audit_event(db, site, "reactivated", operator_id)
    db.commit()
    return get_site(site_id, db)


@router.put("/{site_id}/suggestion-mode", response_model=SiteSuggestionModeState)
def update_suggestion_mode(
    site_id: int,
    payload: SiteSuggestionModeUpdate,
    db: Session = Depends(get_db),
) -> SiteSuggestionModeState:
    _get_site_or_404(db, site_id)
    raise HTTPException(
        409,
        "Hybrid/BM25 is the global suggestion method and cannot be changed per site",
    )


@router.delete("/{site_id}", status_code=204)
def delete_site(site_id: int, db: Session = Depends(get_db)) -> None:
    db.delete(_get_site_or_404(db, site_id))  # ON DELETE CASCADE takes everything else
    db.commit()


@router.get("/{site_id}/articles", response_model=list[ArticleOut])
def list_articles(
    site_id: int,
    orphans: bool = False,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Article]:
    _get_site_or_404(db, site_id)
    query = select(Article).where(
        Article.site_id == site_id,
        Article.is_active.is_(True),
    )
    if orphans:  # Expired links do not count (Phase 0, finding 3).
        query = query.where(
            ~exists().where(
                InternalLink.target_article_id == Article.id,
                InternalLink.is_active.is_(True),
            )
        )
    return db.scalars(query.order_by(Article.id).limit(limit).offset(offset)).all()
