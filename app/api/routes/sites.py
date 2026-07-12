from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Article, InternalLink, Site
from app.schemas.site import ArticleOut, SiteCreate, SiteOut
from app.services.ingestion_service import latest_run

router = APIRouter(prefix="/sites", tags=["sites"])


def _get_site_or_404(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, f"site {site_id} not found")
    return site


@router.post("", status_code=201, response_model=SiteOut)
def create_site(payload: SiteCreate, db: Session = Depends(get_db)) -> Site:
    if db.scalar(select(Site).where(Site.base_url == payload.base_url)):
        raise HTTPException(409, "a site with this base_url already exists")
    site = Site(**payload.model_dump())
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


@router.get("", response_model=list[SiteOut])
def list_sites(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)) -> list[SiteOut]:
    sites = db.scalars(select(Site).order_by(Site.id).limit(limit).offset(offset)).all()
    out = []
    for site in sites:
        item = SiteOut.model_validate(site)
        run = latest_run(db, site.id)
        item.last_ingestion_status = run.status if run else None
        out.append(item)
    return out


@router.get("/{site_id}", response_model=SiteOut)
def get_site(site_id: int, db: Session = Depends(get_db)) -> SiteOut:
    site = _get_site_or_404(db, site_id)
    item = SiteOut.model_validate(site)
    run = latest_run(db, site.id)
    item.last_ingestion_status = run.status if run else None
    return item


@router.delete("/{site_id}", status_code=204)
def delete_site(site_id: int, db: Session = Depends(get_db)) -> None:
    db.delete(_get_site_or_404(db, site_id))  # ON DELETE CASCADE takes everything else
    db.commit()


@router.get("/{site_id}/articles", response_model=list[ArticleOut])
def list_articles(
    site_id: int,
    orphans: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[Article]:
    _get_site_or_404(db, site_id)
    query = select(Article).where(Article.site_id == site_id)
    if orphans:  # no internal link points to them
        query = query.where(~exists().where(InternalLink.target_article_id == Article.id))
    return db.scalars(query.order_by(Article.id).limit(limit).offset(offset)).all()
