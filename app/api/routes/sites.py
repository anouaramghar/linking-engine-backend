from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.config import settings
from app.models import Article, IngestionRun, InternalLink, Site, Suggestion
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
    active_counts_by_source = (
        select(
            Suggestion.site_id.label("site_id"),
            Suggestion.source_article_id.label("source_article_id"),
            func.count(Suggestion.id).label("active_count"),
        )
        .join(Article, Article.id == Suggestion.source_article_id)
        .where(
            Suggestion.site_id.in_(site_ids),
            Article.is_active.is_(True),
            Suggestion.status.in_(("pending", "approved", "applying")),
        )
        .group_by(Suggestion.site_id, Suggestion.source_article_id)
        .subquery()
    )
    used_slots = {
        site_id: int(slot_count)
        for site_id, slot_count in db.execute(
            select(
                active_counts_by_source.c.site_id,
                func.sum(
                    func.least(
                        active_counts_by_source.c.active_count,
                        settings.hybrid_max_suggestions_per_article,
                    )
                ),
            ).group_by(active_counts_by_source.c.site_id)
        )
    }
    suggestion_slots = {
        site_id: (
            article_count * settings.hybrid_max_suggestions_per_article
            - used_slots.get(site_id, 0)
        )
        for site_id, article_count in article_counts.items()
    }
    return article_counts, internal_link_counts, suggestion_slots


def _suggestion_mode_state(_site: Site) -> SiteSuggestionModeState:
    """Backward-compatible API state for the now-global Hybrid method."""
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
    suggestion_slots_available: int,
    run: IngestionRun | None,
) -> SiteOut:
    item = SiteOut.model_validate(site)
    mode = _suggestion_mode_state(site)
    item.suggestion_mode = mode.suggestion_mode
    item.suggestion_mode_managed = mode.suggestion_mode_managed
    item.suggestion_comparison_enabled = mode.suggestion_comparison_enabled
    item.suggestion_slots_available = max(0, suggestion_slots_available)
    item.article_count = article_count
    item.internal_link_count = internal_link_count
    if run is not None:
        item.last_ingestion_status = run.status
        item.last_crawl_at = run.finished_at or run.started_at
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
    article_counts, internal_link_counts, suggestion_slots = _site_counts(
        db, [site.id for site in sites]
    )
    out = []
    for site in sites:
        run = latest_run(db, site.id)
        out.append(
            _site_out(
                site,
                article_count=article_counts.get(site.id, 0),
                internal_link_count=internal_link_counts.get(site.id, 0),
                suggestion_slots_available=suggestion_slots.get(site.id, 0),
                run=run,
            )
        )
    return out


@router.get("/{site_id}", response_model=SiteOut)
def get_site(site_id: int, db: Session = Depends(get_db)) -> SiteOut:
    site = _get_site_or_404(db, site_id)
    article_counts, internal_link_counts, suggestion_slots = _site_counts(db, [site.id])
    run = latest_run(db, site.id)
    return _site_out(
        site,
        article_count=article_counts.get(site.id, 0),
        internal_link_count=internal_link_counts.get(site.id, 0),
        suggestion_slots_available=suggestion_slots.get(site.id, 0),
        run=run,
    )


@router.put(
    "/{site_id}/suggestion-mode",
    response_model=SiteSuggestionModeState,
    include_in_schema=False,
)
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
