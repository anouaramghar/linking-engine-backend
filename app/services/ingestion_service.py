"""Ingestion pipeline: connector -> idempotent upsert -> internal link graph (sequence 4.1)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import ArticleData, TaxonomyData
from app.connectors.registry import get_connector
from app.db import SessionLocal, engine
from app.models import (
    Article,
    ArticleTaxonomy,
    IngestionRun,
    InternalLink,
    Site,
    Suggestion,
    Taxonomy,
)
from app.services.job_service import record_progress_durably
from app.services.pool_source_policy import PoolSourceFetchError

# Advisory-lock namespace registry: 0x4C41 article upsert, 0x4C49 shared content
# snapshot, 0x4C4A enqueue, and 0x4C50 publication.
_ARTICLE_UPSERT_LOCK_NAMESPACE = 0x4C41  # "LA"
_INGESTION_LOCK_NAMESPACE = 0x4C49  # "LI", shared with suggestion_service


@contextmanager
def _site_ingestion_lock(site_id: int) -> Iterator[None]:
    # A dedicated connection keeps the session-level lock for the complete crawl
    # and atomic reconciliation transaction.
    with engine.connect() as lock_connection:
        acquired = lock_connection.execute(
            select(func.pg_try_advisory_lock(_INGESTION_LOCK_NAMESPACE, site_id))
        ).scalar_one()
        if not acquired:
            raise RuntimeError(f"ingestion already running for site {site_id}")
        try:
            yield
        finally:
            lock_connection.execute(
                select(func.pg_advisory_unlock(_INGESTION_LOCK_NAMESPACE, site_id))
            ).scalar_one()


def _validate_snapshot_completeness(
    db: Session, site_id: int, run_id: int, article_count: int
) -> None:
    if article_count == 0:
        raise ValueError(
            f"crawl returned zero articles for site {site_id}; snapshot not reconciled"
        )

    previous_count = db.scalar(
        select(IngestionRun.articles_upserted)
        .where(
            IngestionRun.site_id == site_id,
            IngestionRun.status == "succeeded",
            IngestionRun.id != run_id,
        )
        .order_by(IngestionRun.id.desc())
        .limit(1)
    )
    minimum_ratio = settings.ingestion_min_previous_ratio
    if previous_count and article_count < previous_count * minimum_ratio:
        raise ValueError(
            f"crawl returned {article_count} articles for site {site_id}, below "
            f"{minimum_ratio:.0%} of the previous successful crawl ({previous_count}); "
            "snapshot not reconciled"
        )


def normalize_url(url: str) -> str:
    """Comparison key: scheme-insensitive, no fragment/query, no trailing slash."""
    p = urlparse(url)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}"


def _upsert_article(db: Session, site_id: int, art: ArticleData, run_id: int | None = None) -> int:
    db.execute(select(func.pg_advisory_xact_lock(_ARTICLE_UPSERT_LOCK_NAMESPACE, site_id)))

    identity_filters = [Article.url == art.url]
    if art.external_id is not None:
        identity_filters.append(Article.external_id == art.external_id)

    matches = db.scalars(
        select(Article).where(Article.site_id == site_id, or_(*identity_filters)).with_for_update()
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
        if run_id is not None:
            # New rows stay hidden until successful reconciliation (Phase 0, finding 3).
            article.is_active = False
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
    if run_id is not None:
        article.last_seen_run_id = run_id
    db.flush()
    return article.id


def _upsert_taxonomies(
    db: Session,
    site_id: int,
    article_id: int,
    taxonomies: list[TaxonomyData],
    run_id: int,
) -> None:
    for tax in taxonomies:
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
            .values(
                article_id=article_id,
                taxonomy_id=tax_id,
                last_seen_run_id=run_id,
            )
            .on_conflict_do_update(
                index_elements=["article_id", "taxonomy_id"],
                set_={"last_seen_run_id": run_id},
            )
        )


def _upsert_link(db: Session, source_id: int, target_id: int, run_id: int) -> None:
    db.execute(
        pg_insert(InternalLink)
        .values(
            source_article_id=source_id,
            target_article_id=target_id,
            is_active=True,
            last_seen_run_id=run_id,
        )
        .on_conflict_do_update(
            index_elements=["source_article_id", "target_article_id"],
            # first_seen_at stays immutable for history (Phase 0, finding 3).
            set_={
                "is_active": True,
                "last_seen_at": func.now(),
                "last_seen_run_id": run_id,
            },
        )
    )


def _reconcile_snapshot(db: Session, site_id: int, run_id: int) -> None:
    site_article_ids = select(Article.id).where(Article.site_id == site_id)
    inactive_article_ids = select(Article.id).where(
        Article.site_id == site_id,
        Article.last_seen_run_id.is_distinct_from(run_id),
    )
    taxonomy_reporter_ids = select(ArticleTaxonomy.article_id).where(
        ArticleTaxonomy.last_seen_run_id == run_id
    )
    db.execute(
        update(Article)
        .where(
            Article.site_id == site_id,
            Article.last_seen_run_id == run_id,
            Article.is_active.is_(False),
        )
        .values(is_active=True)
    )
    db.execute(
        update(Article)
        .where(
            Article.site_id == site_id,
            Article.last_seen_run_id.is_distinct_from(run_id),
            Article.is_active.is_(True),
        )
        .values(is_active=False)
    )
    db.execute(
        update(Suggestion)
        .where(
            Suggestion.status.in_(("pending", "approved")),
            or_(
                Suggestion.source_article_id.in_(inactive_article_ids),
                Suggestion.target_article_id.in_(inactive_article_ids),
            ),
        )
        .values(status="expired")
    )
    db.execute(
        update(InternalLink)
        .where(
            InternalLink.source_article_id.in_(site_article_ids),
            InternalLink.last_seen_run_id.is_distinct_from(run_id),
            InternalLink.is_active.is_(True),
        )
        .values(is_active=False)
    )
    db.execute(
        delete(ArticleTaxonomy).where(
            ArticleTaxonomy.article_id.in_(site_article_ids),
            ArticleTaxonomy.last_seen_run_id.is_distinct_from(run_id),
            or_(
                ArticleTaxonomy.article_id.in_(inactive_article_ids),
                ArticleTaxonomy.article_id.in_(taxonomy_reporter_ids),
            ),
        )
    )


def _run_ingestion_locked(site_id: int, job_run_id: int | None = None) -> dict:
    """RQ task body. A run never stays 'running' (sequence 4.1 alt success/failure)."""
    db = SessionLocal()
    run = IngestionRun(site_id=site_id, job_run_id=job_run_id)
    db.add(run)
    db.commit()
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)

        # Snapshot writes remain in one transaction so a failed crawl cannot alter
        # live content. This deliberately trades bounded, resumable batch commits
        # for atomicity; use staging and promotion before scaling to very large crawls.
        url_to_id: dict[str, int] = {}
        outbound: list[tuple[int, list[str]]] = []
        article_ids: set[int] = set()
        last_progress_count = 0
        try:
            articles_iterator = iter(connector.fetch_articles())
        except Exception as error:
            if site.platform == "pool":
                raise PoolSourceFetchError(str(error)) from error
            raise
        while True:
            try:
                art = next(articles_iterator)
            except StopIteration:
                break
            except Exception as error:
                if site.platform == "pool":
                    raise PoolSourceFetchError(str(error)) from error
                raise
            article_id = _upsert_article(db, site_id, art, run.id)
            _upsert_taxonomies(db, site_id, article_id, art.taxonomies, run.id)
            url_to_id[normalize_url(art.url)] = article_id
            outbound.append((article_id, art.outbound_internal_urls))
            article_ids.add(article_id)
            articles = len(article_ids)
            if articles % 50 == 0 and articles != last_progress_count:
                # Keep progress visible without committing any live snapshot data.
                record_progress_durably(
                    job_run_id,
                    stage="crawling",
                    articles=articles,
                )
                last_progress_count = articles

        articles = len(article_ids)

        try:
            _validate_snapshot_completeness(db, site_id, run.id, articles)
        except ValueError as error:
            if site.platform == "pool":
                raise PoolSourceFetchError(str(error)) from error
            raise

        # Resolve links once all articles are known (forward references)
        record_progress_durably(job_run_id, stage="resolving_links")
        links = 0
        seen: set[tuple[int, int]] = set()
        resolve_internal_url = getattr(connector, "resolve_internal_url", None)
        for source_id, urls in outbound:
            for url in urls:
                target_id = url_to_id.get(normalize_url(url))
                if target_id is None and resolve_internal_url is not None:
                    target_id = url_to_id.get(normalize_url(resolve_internal_url(url)))
                if target_id and target_id != source_id and (source_id, target_id) not in seen:
                    _upsert_link(db, source_id, target_id, run.id)
                    seen.add((source_id, target_id))
                    links += 1

        # Reconciliation is success-gated after link resolution (Phase 0, finding 3).
        record_progress_durably(
            job_run_id,
            stage="reconciling",
            articles=articles,
            links=links,
        )
        _reconcile_snapshot(db, site_id, run.id)
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


def run_ingestion(site_id: int, job_run_id: int | None = None) -> dict:
    """Run one site crawl at a time and record lock contention as a failed run."""
    lock_acquired = False
    try:
        with _site_ingestion_lock(site_id):
            lock_acquired = True
            return _run_ingestion_locked(site_id, job_run_id)
    except Exception as error:
        if lock_acquired:
            raise
        db = SessionLocal()
        try:
            run = IngestionRun(
                site_id=site_id,
                job_run_id=job_run_id,
                status="failed",
                error=str(error)[:2000],
                finished_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.commit()
        finally:
            db.close()
        raise


def latest_run(db: Session, site_id: int) -> IngestionRun | None:
    return db.scalars(
        select(IngestionRun)
        .where(IngestionRun.site_id == site_id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ).first()
