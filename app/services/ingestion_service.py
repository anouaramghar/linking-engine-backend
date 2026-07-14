"""Ingestion pipeline: connector -> idempotent upsert -> internal link graph (sequence 4.1)."""

from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.connectors.base import ArticleData
from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.models import Article, ArticleTaxonomy, IngestionRun, InternalLink, Site, Taxonomy

_ARTICLE_UPSERT_LOCK_NAMESPACE = 0x4C4D  # "LM"


def normalize_url(url: str) -> str:
    """Comparison key: scheme-insensitive, no fragment/query, no trailing slash."""
    p = urlparse(url)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}"


def _upsert_article(db: Session, site_id: int, art: ArticleData) -> int:
    db.execute(select(func.pg_advisory_xact_lock(_ARTICLE_UPSERT_LOCK_NAMESPACE, site_id)))

    identity_filters = [Article.url == art.url]
    if art.external_id is not None:
        identity_filters.append(Article.external_id == art.external_id)

    matches = db.scalars(
        select(Article)
        .where(Article.site_id == site_id, or_(*identity_filters))
        .with_for_update()
    ).all()
    by_url = next((article for article in matches if article.url == art.url), None)
    by_external_id = next(
        (
            article
            for article in matches
            if art.external_id is not None and article.external_id == art.external_id
        ),
        None,
    )
    article = by_external_id or by_url

    if article is None:
        article = Article(site_id=site_id, url=art.url, title="", content_text="")
        db.add(article)
    elif by_external_id is not None and by_url is not None and by_external_id.id != by_url.id:
        previous_url = by_external_id.url
        by_url.url = f"linkmesh://url-swap/{site_id}/{by_url.id}"
        db.flush()
        by_external_id.url = art.url
        db.flush()
        by_url.url = previous_url

    if art.external_id is not None:
        article.external_id = art.external_id
    article.url = art.url
    article.title = art.title
    article.content_text = art.content_text
    article.content_html = art.content_html
    article.language = art.language
    article.published_at = art.published_at
    db.flush()
    return article.id


def _upsert_taxonomies(db: Session, site_id: int, article_id: int, art: ArticleData) -> None:
    for tax in art.taxonomies:
        tax_id = db.execute(
            pg_insert(Taxonomy)
            .values(site_id=site_id, kind=tax.kind, name=tax.name, external_id=tax.external_id)
            .on_conflict_do_update(  # do_nothing returns no id; harmless update instead
                index_elements=["site_id", "kind", "name"], set_={"external_id": tax.external_id}
            )
            .returning(Taxonomy.id)
        ).scalar_one()
        db.execute(
            pg_insert(ArticleTaxonomy)
            .values(article_id=article_id, taxonomy_id=tax_id)
            .on_conflict_do_nothing()
        )


def _upsert_link(db: Session, source_id: int, target_id: int) -> None:
    db.execute(
        pg_insert(InternalLink)
        .values(source_article_id=source_id, target_article_id=target_id)
        .on_conflict_do_update(
            index_elements=["source_article_id", "target_article_id"],
            set_={"last_seen_at": func.now()},  # first_seen_at untouched — temporal split
        )
    )


def run_ingestion(site_id: int) -> dict:
    """RQ task body. A run never stays 'running' (sequence 4.1 alt success/failure)."""
    db = SessionLocal()
    run = IngestionRun(site_id=site_id)
    db.add(run)
    db.commit()
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)

        url_to_id: dict[str, int] = {}
        outbound: list[tuple[int, list[str]]] = []
        articles = 0
        for art in connector.fetch_articles():
            article_id = _upsert_article(db, site_id, art)
            _upsert_taxonomies(db, site_id, article_id, art)
            url_to_id[normalize_url(art.url)] = article_id
            outbound.append((article_id, art.outbound_internal_urls))
            articles += 1
            if articles % 50 == 0:
                db.commit()  # batch commit — resumable, bounded transaction size

        # Resolve links once all articles are known (forward references)
        links = 0
        seen: set[tuple[int, int]] = set()
        for source_id, urls in outbound:
            for url in urls:
                target_id = url_to_id.get(normalize_url(url))
                if target_id and target_id != source_id and (source_id, target_id) not in seen:
                    _upsert_link(db, source_id, target_id)
                    seen.add((source_id, target_id))
                    links += 1

        run.status = "succeeded"
        run.articles_upserted = articles
        run.links_found = links
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {"articles": articles, "links": links}
    except Exception as e:
        db.rollback()
        run.status = "failed"
        run.error = str(e)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise
    finally:
        db.close()


def latest_run(db: Session, site_id: int) -> IngestionRun | None:
    return db.scalars(
        select(IngestionRun)
        .where(IngestionRun.site_id == site_id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ).first()
