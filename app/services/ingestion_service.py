"""Ingestion pipeline: connector -> idempotent upsert -> internal link graph (sequence 4.1)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlparse, urlunparse

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from lxml import html as lxml_html

from app.config import settings
from app.connectors.base import ArticleData, DiscoveryObservation, TaxonomyData
from app.connectors.http_limits import check_crawl_deadline
from app.connectors.registry import get_connector
from app.connectors.url_guard import UnsafeURLError, validate_url
from app.db import SessionLocal, engine
from app.ml.external.cleaning import normalize_external_url
from app.models import (
    Article,
    ArticleTaxonomy,
    IngestionRun,
    Site,
    Taxonomy,
)
from app.services.crawl_snapshot import CrawlSnapshot, normalize_url
from app.services.job_service import JobCancelled, check_job_cancellation, record_progress_durably
from app.services.pool_source_policy import PoolSourceFetchError

# Advisory-lock namespace registry: 0x4C41 article upsert, 0x4C42 PBN domain
# check, 0x4C49 shared content snapshot, 0x4C4A enqueue, and 0x4C50 publication.
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


def _import_canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _text_from_import_html(content_html: str | None) -> str:
    if not content_html:
        return ""
    try:
        return " ".join(lxml_html.fromstring(content_html).text_content().split())
    except (ValueError, TypeError):
        return ""


def import_articles(
    db: Session,
    site: Site,
    rows,
    *,
    replace_snapshot: bool = False,
) -> dict:
    """Import normalized or Screaming Frog article rows into the article graph.

    The import is a source snapshot only when ``replace_snapshot`` is explicit.
    The default enriches/updates the supplied URLs without deactivating pages
    absent from a partial CSV export.
    """
    skipped: list[dict] = []
    rejected: list[dict] = []
    observations: list[DiscoveryObservation] = []
    imported = 0
    updated = 0
    seen_urls: set[str] = set()
    with _site_ingestion_lock(site.id):
        run = IngestionRun(site_id=site.id, status="running")
        db.add(run)
        db.commit()
        snapshot = CrawlSnapshot(site_id=site.id, run_id=run.id)
        try:
            for row_number, row in enumerate(rows, start=1):
                values = row.model_dump() if hasattr(row, "model_dump") else dict(row)
                raw_url = (values.get("url") or "").strip()
                if not raw_url:
                    reason = "url is required"
                    rejected.append({"row": row_number, "url": None, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=f"import-row:{row_number}",
                            state="skipped",
                            reason_code="missing_url",
                            reason_detail=reason,
                        )
                    )
                    continue
                try:
                    validate_url(
                        raw_url,
                        allow_private=settings.allow_unsafe_crawl_targets,
                        same_host_as=site.base_url,
                        resolve_dns=False,
                    )
                except UnsafeURLError as error:
                    reason = str(error)
                    rejected.append({"row": row_number, "url": raw_url, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=raw_url,
                            state="skipped",
                            reason_code="unsafe_url",
                            reason_detail=reason,
                        )
                    )
                    continue
                url = _import_canonical_url(values.get("canonical_url") or raw_url)
                try:
                    validate_url(
                        url,
                        allow_private=settings.allow_unsafe_crawl_targets,
                        same_host_as=site.base_url,
                        resolve_dns=False,
                    )
                except UnsafeURLError as error:
                    reason = f"canonical URL is unsafe: {error}"
                    rejected.append({"row": row_number, "url": raw_url, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=raw_url,
                            state="skipped",
                            reason_code="unsafe_canonical",
                            reason_detail=reason,
                        )
                    )
                    continue
                key = normalize_url(url)
                if key in seen_urls:
                    reason = "duplicate URL in this import"
                    skipped.append({"row": row_number, "url": raw_url, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=url,
                            state="skipped",
                            reason_code="duplicate_import_url",
                            reason_detail=reason,
                        )
                    )
                    continue
                seen_urls.add(key)
                status_code = values.get("status_code")
                if status_code is not None and status_code != 200:
                    reason = f"HTTP status is {status_code}"
                    skipped.append({"row": row_number, "url": raw_url, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=url,
                            state="skipped",
                            reason_code="http_error",
                            reason_detail=reason,
                            http_status=status_code,
                        )
                    )
                    continue
                indexability = (values.get("indexability") or "").strip().lower()
                if indexability and indexability not in {"indexable", "index"}:
                    reason = f"indexability is {values['indexability']}"
                    skipped.append({"row": row_number, "url": raw_url, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=url,
                            state="skipped",
                            reason_code="noindex",
                            reason_detail=reason,
                        )
                    )
                    continue
                content_html = values.get("content_html")
                title = (values.get("title") or "").strip() or url
                content_text = (values.get("content_text") or "").strip()
                if not content_text:
                    content_text = _text_from_import_html(content_html) or title
                if len(content_text) > settings.crawl_max_article_chars:
                    reason = f"content exceeded {settings.crawl_max_article_chars} characters"
                    rejected.append({"row": row_number, "url": raw_url, "reason": reason})
                    observations.append(
                        DiscoveryObservation(
                            url=url,
                            state="skipped",
                            reason_code="article_too_large",
                            reason_detail=reason,
                        )
                    )
                    continue
                existing_id = db.scalar(
                    select(Article.id).where(Article.site_id == site.id, Article.url == url)
                )
                article = ArticleData(
                    url=url,
                    title=title,
                    content_text=content_text,
                    content_html=content_html,
                    discovered_from=values.get("discovered_from"),
                    discovery_depth=values.get("discovery_depth") or 0,
                    canonical_url=values.get("canonical_url"),
                    outbound_internal_links=values.get("outbound_internal_links") or [],
                )
                snapshot.record_accepted_article(article)
                article_id = _upsert_article(db, site.id, article, run.id)
                stored = db.get(Article, article_id)
                if stored is not None and not replace_snapshot:
                    stored.is_active = True
                snapshot.stage_article(article, article_id)
                if existing_id is None:
                    imported += 1
                else:
                    updated += 1

            if replace_snapshot:
                snapshot.validate_completeness(db)
            links = snapshot.resolve_links(db)
            if replace_snapshot:
                snapshot.promote(db)
            run.status = "succeeded"
            run.articles_upserted = snapshot.article_count
            run.links_found = links
            snapshot.persist_diagnostics(db, run, observations=observations)
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            return {
                "ingestion_run_id": run.id,
                "imported": imported,
                "updated": updated,
                "links_found": links,
                "skipped": skipped,
                "rejected": rejected,
                "diagnostic_summary": run.diagnostic_summary,
            }
        except Exception as error:
            db.rollback()
            run.status = "failed"
            run.error = str(error)[:2000]
            snapshot.persist_diagnostics(db, run, observations=observations)
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            raise


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


def _run_ingestion_locked(site_id: int, job_run_id: int | None = None) -> dict:
    """RQ task body. A run never stays 'running' (sequence 4.1 alt success/failure)."""
    db = SessionLocal()
    run = IngestionRun(site_id=site_id, job_run_id=job_run_id)
    db.add(run)
    db.commit()
    connector = None
    snapshot = CrawlSnapshot(site_id=site_id, run_id=run.id)
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)

        # Snapshot writes remain in one transaction so a failed crawl cannot alter
        # live content. This deliberately trades bounded, resumable batch commits
        # for atomicity; use staging and promotion before scaling to very large crawls.
        crawl_started_at = monotonic()
        external_urls_seen: set[str] = set()
        articles_seen = 0
        total_outbound_links = 0
        last_progress_count = 0
        try:
            articles_iterator = iter(connector.fetch_articles())
        except Exception as error:
            if site.platform == "pool":
                raise PoolSourceFetchError(str(error)) from error
            raise
        while True:
            check_job_cancellation(job_run_id)
            check_crawl_deadline(crawl_started_at)
            try:
                art = next(articles_iterator)
            except StopIteration:
                break
            except Exception as error:
                if site.platform == "pool":
                    raise PoolSourceFetchError(str(error)) from error
                raise
            articles_seen += 1
            if articles_seen > settings.crawl_max_articles:
                raise ValueError(f"crawl article count exceeded {settings.crawl_max_articles}")
            if len(art.content_text) > settings.crawl_max_article_chars:
                raise ValueError(
                    f"article content exceeded {settings.crawl_max_article_chars} characters"
                )
            if site.platform == "pool":
                try:
                    normalized_external_url = normalize_external_url(art.url)
                except ValueError as error:
                    raise PoolSourceFetchError(str(error)) from error
                # Feeds often repeat the same article with a tracking URL or a
                # different query order. Keep the first (normally newest) entry
                # and never let those aliases create distinct Article rows.
                if normalized_external_url in external_urls_seen:
                    continue
                external_urls_seen.add(normalized_external_url)
                art = art.model_copy(update={"url": normalized_external_url})
            if len(art.outbound_internal_links) > settings.crawl_max_links_per_article:
                raise ValueError(
                    f"article link count exceeded {settings.crawl_max_links_per_article}"
                )
            total_outbound_links += len(art.outbound_internal_links)
            if total_outbound_links > settings.crawl_max_total_links:
                raise ValueError(f"crawl link count exceeded {settings.crawl_max_total_links}")
            snapshot.record_accepted_article(art)
            article_id = _upsert_article(db, site_id, art, run.id)
            _upsert_taxonomies(db, site_id, article_id, art.taxonomies, run.id)
            snapshot.stage_article(art, article_id)
            articles = snapshot.article_count
            if articles % 50 == 0 and articles != last_progress_count:
                # Keep progress visible without committing any live snapshot data.
                record_progress_durably(
                    job_run_id,
                    stage="crawling",
                    articles=articles,
                )
                last_progress_count = articles

        articles = snapshot.article_count

        check_job_cancellation(job_run_id)
        try:
            snapshot.validate_completeness(db)
        except ValueError as error:
            if site.platform == "pool":
                raise PoolSourceFetchError(str(error)) from error
            raise

        # Resolve links once all articles are known (forward references)
        check_job_cancellation(job_run_id)
        record_progress_durably(job_run_id, stage="resolving_links")
        resolve_internal_url = getattr(connector, "resolve_internal_url", None)

        def check_crawl_interruption() -> None:
            check_job_cancellation(job_run_id)
            check_crawl_deadline(crawl_started_at)

        links = snapshot.resolve_links(
            db,
            resolve_internal_url=resolve_internal_url,
            check_deadline=check_crawl_interruption,
        )

        # Reconciliation is success-gated after link resolution (Phase 0, finding 3).
        check_job_cancellation(job_run_id)
        record_progress_durably(
            job_run_id,
            stage="reconciling",
            articles=articles,
            links=links,
        )
        check_job_cancellation(job_run_id)
        snapshot.promote(db)
        run.status = "succeeded"
        run.articles_upserted = articles
        run.links_found = links
        snapshot.persist_diagnostics(db, run, connector)
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {"articles": articles, "links": links}
    except JobCancelled as e:
        db.rollback()
        run.status = "cancelled"
        run.error = str(e)[:2000]
        if connector is not None:
            snapshot.persist_diagnostics(db, run, connector)
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise
    except Exception as e:
        db.rollback()
        run.status = "failed"
        run.error = str(e)[:2000]
        if connector is not None:
            snapshot.persist_diagnostics(db, run, connector)
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
            status = "failed"
            try:
                check_job_cancellation(job_run_id)
            except JobCancelled:
                status = "cancelled"
            run = IngestionRun(
                site_id=site_id,
                job_run_id=job_run_id,
                status=status,
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
