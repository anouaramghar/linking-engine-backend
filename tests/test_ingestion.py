"""Ingestion pipeline against a stub connector — no network, real database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from sqlalchemy import delete, event, func, select

import app.services.ingestion_service as ingestion_service
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    OutboundLink,
    SiteMetadata,
    TaxonomyData,
)
from app.db import SessionLocal, engine
from app.models import (
    Article,
    ArticleTaxonomy,
    IngestionRun,
    InternalLink,
    JobRun,
    Site,
    Suggestion,
    Taxonomy,
)
from app.services.job_service import run_durably


class StubConnector(ContentConnector):
    def fetch_articles(self):
        base = self.site.base_url
        yield ArticleData(
            url=f"{base}/a",
            title="Article A",
            content_text="text a",
            external_id="1",
            taxonomies=[TaxonomyData(kind="category", name="SEO")],
            outbound_internal_links=[
                OutboundLink(url=f"{base}/b", anchor_text="to B"),
                OutboundLink(url=f"{base}/b#frag"),
                OutboundLink(url=f"{base}/missing"),
            ],
        )
        yield ArticleData(
            url=f"{base}/b/",
            title="Article B",
            content_text="text b",
            external_id="2",
            outbound_internal_links=[OutboundLink(url=f"{base}/a", anchor_text="back to A")],
        )

    def fetch_article_by_url(self, url):
        return None

    def get_site_metadata(self):
        return SiteMetadata(name="stub", base_url=self.site.base_url, platform="wordpress")

    def supports_incremental_sync(self):
        return False

    def apply_links(self, suggestions, *, dry_run=False):
        pass


class ReducedStubConnector(StubConnector):
    def fetch_articles(self):
        yield ArticleData(
            url=f"{self.site.base_url}/a",
            title="Article A",
            content_text="text a",
            external_id="1",
        )


class ChangedTaxonomyStubConnector(ReducedStubConnector):
    def fetch_articles(self):
        yield ArticleData(
            url=f"{self.site.base_url}/a",
            title="Article A",
            content_text="text a",
            external_id="1",
            taxonomies=[TaxonomyData(kind="category", name="Content")],
        )


class EmptyStubConnector(StubConnector):
    def fetch_articles(self):
        return iter(())


class FourArticleStubConnector(StubConnector):
    def fetch_articles(self):
        for index in range(4):
            yield ArticleData(
                url=f"{self.site.base_url}/article-{index}",
                title=f"Article {index}",
                content_text=f"text {index}",
                external_id=str(index + 1),
            )


class DuplicateArticleStubConnector(StubConnector):
    def fetch_articles(self):
        article = ArticleData(
            url=f"{self.site.base_url}/article-0",
            title="Duplicate article",
            content_text="duplicate",
            external_id="1",
        )
        for _ in range(4):
            yield article


class ExternalAliasStubConnector(StubConnector):
    def fetch_articles(self):
        yield ArticleData(
            url="https://EXAMPLE.com:443/report?id=7&utm_source=feed#first",
            title="Canonical article",
            content_text="first copy wins",
            external_id="canonical",
        )
        yield ArticleData(
            url="https://example.com/report?utm_medium=email&id=7",
            title="Tracked alias",
            content_text="duplicate copy",
            external_id="alias",
        )


class FailingStubConnector(ReducedStubConnector):
    def fetch_articles(self):
        yield ArticleData(
            url=f"{self.site.base_url}/a",
            title="Uncommitted Article A",
            content_text="content from failed crawl",
            external_id="1",
            taxonomies=[TaxonomyData(kind="category", name="New")],
        )
        for index in range(49):
            yield ArticleData(
                url=f"{self.site.base_url}/partial-{index}",
                title=f"Partial {index}",
                content_text="partial",
                external_id=f"partial-{index}",
            )
        raise RuntimeError("crawl interrupted")


def test_ingestion_idempotent(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))

    # first run
    result = ingestion_service.run_ingestion(site.id)
    assert result == {"articles": 2, "links": 2}  # a->b (dedup'd fragment), b->a; /missing dropped

    # second run: updates, never duplicates
    result = ingestion_service.run_ingestion(site.id)
    assert result == {"articles": 2, "links": 2}

    articles = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    assert len(articles) == 2
    links = db.scalars(
        select(InternalLink).where(InternalLink.source_article_id.in_([a.id for a in articles]))
    ).all()
    assert len(links) == 2
    assert all(link.last_seen_at >= link.first_seen_at for link in links)

    taxonomies = db.scalars(select(Taxonomy).where(Taxonomy.site_id == site.id)).all()
    assert [t.name for t in taxonomies] == ["SEO"]

    runs = db.scalars(select(IngestionRun).where(IngestionRun.site_id == site.id)).all()
    assert len(runs) == 2
    assert all(r.status == "succeeded" and r.finished_at is not None for r in runs)


def test_ingestion_stores_the_anchor_text_of_each_link(db, site, monkeypatch):
    """The words an editor chose are the anchor-distribution report's only input.

    The connector had always read them and the column had always existed; the
    resolution step simply never passed one to the other, so every row was NULL
    and there was nothing to report on.
    """
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)

    urls = dict(
        db.execute(select(Article.id, Article.url).where(Article.site_id == site.id)).all()
    )
    anchors = {
        (urls[link.source_article_id], urls[link.target_article_id]): link.anchor_text
        for link in db.scalars(
            select(InternalLink).where(InternalLink.source_article_id.in_(urls))
        )
    }
    assert anchors == {
        (f"{site.base_url}/a", f"{site.base_url}/b/"): "to B",
        (f"{site.base_url}/b/", f"{site.base_url}/a"): "back to A",
    }


def test_durable_ingestion_records_final_progress(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    job_run = JobRun(site_id=site.id, kind="ingestion")
    db.add(job_run)
    db.commit()

    result = run_durably(job_run.id, ingestion_service.run_ingestion, site.id)

    assert result == {"articles": 2, "links": 2}
    db.expire_all()
    job_run = db.get(JobRun, job_run.id)
    ingestion_run = db.scalar(
        select(IngestionRun).where(IngestionRun.site_id == site.id)
    )
    assert job_run.status == "succeeded"
    assert job_run.progress == {
        "stage": "reconciling",
        "articles": 2,
        "links": 2,
    }
    assert ingestion_run.job_run_id == job_run.id
    assert job_run.progress_at is not None


def test_progress_write_failure_does_not_fail_ingestion(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    job_run = JobRun(site_id=site.id, kind="ingestion")
    db.add(job_run)
    db.commit()

    def fail_progress_update(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith("update job_runs set progress"):
            raise RuntimeError("progress storage unavailable")

    event.listen(engine, "before_cursor_execute", fail_progress_update)
    try:
        result = run_durably(job_run.id, ingestion_service.run_ingestion, site.id)
    finally:
        event.remove(engine, "before_cursor_execute", fail_progress_update)

    assert result == {"articles": 2, "links": 2}
    db.expire_all()
    job_run = db.get(JobRun, job_run.id)
    assert job_run.status == "succeeded"
    assert job_run.progress is None
    ingestion_run = db.scalar(
        select(IngestionRun).where(IngestionRun.site_id == site.id)
    )
    assert ingestion_run.job_run_id == job_run.id


def test_successful_recrawl_deactivates_article_not_seen(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: ReducedStubConnector(s)
    )
    ingestion_service.run_ingestion(site.id)

    db.expire_all()
    articles = {
        article.external_id: article
        for article in db.scalars(select(Article).where(Article.site_id == site.id)).all()
    }
    runs = db.scalars(
        select(IngestionRun)
        .where(IngestionRun.site_id == site.id)
        .order_by(IngestionRun.id)
    ).all()

    assert articles["1"].is_active is True
    assert articles["2"].is_active is False
    assert getattr(articles["1"], "last_seen_run_id", None) == runs[-1].id
    assert getattr(articles["2"], "last_seen_run_id", None) == runs[0].id


def test_empty_recrawl_fails_without_reconciling_snapshot(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: EmptyStubConnector(s))
    with pytest.raises(ValueError, match="zero articles.*snapshot not reconciled"):
        ingestion_service.run_ingestion(site.id)

    db.expire_all()
    articles = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    runs = db.scalars(
        select(IngestionRun).where(IngestionRun.site_id == site.id).order_by(IngestionRun.id)
    ).all()
    assert all(article.is_active is True for article in articles)
    assert [run.status for run in runs] == ["succeeded", "failed"]
    assert "zero articles" in runs[-1].error


def test_truncated_recrawl_fails_below_previous_success_floor(db, site, monkeypatch):
    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: FourArticleStubConnector(s)
    )
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: ReducedStubConnector(s)
    )
    with pytest.raises(ValueError, match=r"1 articles.*50%.*previous.*\(4\)"):
        ingestion_service.run_ingestion(site.id)

    db.expire_all()
    articles = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    runs = db.scalars(
        select(IngestionRun).where(IngestionRun.site_id == site.id).order_by(IngestionRun.id)
    ).all()
    assert len(articles) == 4
    assert all(article.is_active is True for article in articles)
    assert [run.status for run in runs] == ["succeeded", "failed"]
    assert "snapshot not reconciled" in runs[-1].error


def test_duplicate_records_do_not_bypass_snapshot_completeness(db, site, monkeypatch):
    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: FourArticleStubConnector(s)
    )
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: DuplicateArticleStubConnector(s)
    )
    with pytest.raises(ValueError, match=r"1 articles.*50%.*previous.*\(4\)"):
        ingestion_service.run_ingestion(site.id)

    db.expire_all()
    articles = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    assert len(articles) == 4
    assert all(article.is_active is True for article in articles)


def test_pool_ingestion_normalizes_and_deduplicates_external_article_urls(
    db, monkeypatch
):
    pool = Site(
        name="External pool",
        base_url="https://en.wikipedia.org/wiki/Link_analysis",
        platform="pool",
        crawl_frequency="daily",
        pool_source_approved=True,
    )
    db.add(pool)
    db.commit()
    monkeypatch.setattr(
        ingestion_service,
        "get_connector",
        lambda source: ExternalAliasStubConnector(source),
    )

    try:
        result = ingestion_service.run_ingestion(pool.id)
        db.expire_all()
        articles = db.scalars(select(Article).where(Article.site_id == pool.id)).all()
        stored = [
            (article.url, article.external_id, article.content_text) for article in articles
        ]
    finally:
        # Pool articles participate in every site's candidate corpus. This test
        # creates its own source rather than using the per-test `site` fixture,
        # so it must also remove it even when an assertion or ingestion fails.
        db.rollback()
        db.execute(delete(Site).where(Site.id == pool.id))
        db.commit()

    assert result == {"articles": 1, "links": 0}
    assert stored == [
        ("https://example.com/report?id=7", "canonical", "first copy wins")
    ]


def test_overlapping_ingestion_fails_fast_and_records_failed_run(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))

    with engine.connect() as lock_connection:
        lock_connection.execute(
            select(
                func.pg_advisory_lock(
                    ingestion_service._INGESTION_LOCK_NAMESPACE,
                    site.id,
                )
            )
        ).scalar_one()
        try:
            with pytest.raises(RuntimeError, match=f"ingestion already running for site {site.id}"):
                ingestion_service.run_ingestion(site.id)
        finally:
            lock_connection.execute(
                select(
                    func.pg_advisory_unlock(
                        ingestion_service._INGESTION_LOCK_NAMESPACE,
                        site.id,
                    )
                )
            ).scalar_one()

    db.expire_all()
    run = db.scalar(select(IngestionRun).where(IngestionRun.site_id == site.id))
    assert run.status == "failed"
    assert "ingestion already running" in run.error


def test_successful_recrawl_expires_links_and_preserves_transient_empty_taxonomies(
    db, site, monkeypatch
):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: ReducedStubConnector(s)
    )
    ingestion_service.run_ingestion(site.id)

    db.expire_all()
    links = db.scalars(
        select(InternalLink)
        .join(Article, InternalLink.source_article_id == Article.id)
        .where(Article.site_id == site.id)
    ).all()
    memberships = db.scalars(
        select(ArticleTaxonomy)
        .join(Article, ArticleTaxonomy.article_id == Article.id)
        .where(Article.site_id == site.id)
    ).all()

    assert len(links) == 2
    assert all(link.is_active is False for link in links)
    assert len(memberships) == 1


def test_nonempty_taxonomy_snapshot_removes_stale_memberships(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: ChangedTaxonomyStubConnector(s)
    )
    ingestion_service.run_ingestion(site.id)

    membership_names = db.scalars(
        select(Taxonomy.name)
        .join(ArticleTaxonomy, ArticleTaxonomy.taxonomy_id == Taxonomy.id)
        .join(Article, ArticleTaxonomy.article_id == Article.id)
        .where(Article.site_id == site.id)
    ).all()
    assert membership_names == ["Content"]


def test_reconcile_skips_stable_article_and_link_updates(db, site):
    run = IngestionRun(site_id=site.id)
    db.add(run)
    db.flush()
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/stable-source",
        title="Stable source",
        content_text="source",
        is_active=True,
        last_seen_run_id=run.id,
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/stable-target",
        title="Stable target",
        content_text="target",
        is_active=True,
        last_seen_run_id=run.id,
    )
    stale_inactive = Article(
        site_id=site.id,
        url=f"{site.base_url}/stale-inactive",
        title="Stale inactive",
        content_text="inactive",
        is_active=False,
    )
    db.add_all([source, target, stale_inactive])
    db.flush()
    db.add(
        InternalLink(
            source_article_id=source.id,
            target_article_id=target.id,
            is_active=False,
        )
    )
    db.commit()
    update_rowcounts = {"articles": [], "internal_links": []}

    def record_update_rowcounts(_conn, cursor, statement, _params, _context, _many):
        normalized = statement.lstrip().lower()
        for table in update_rowcounts:
            if normalized.startswith(f"update {table}"):
                update_rowcounts[table].append(cursor.rowcount)

    event.listen(engine, "after_cursor_execute", record_update_rowcounts)
    try:
        ingestion_service._reconcile_snapshot(db, site.id, run.id)
        db.commit()
    finally:
        event.remove(engine, "after_cursor_execute", record_update_rowcounts)

    assert update_rowcounts == {"articles": [0, 0], "internal_links": [0]}
    assert stale_inactive.is_active is False


def test_recrawl_expires_inactive_article_suggestions(db, site, client, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)
    articles = {
        article.external_id: article
        for article in db.scalars(select(Article).where(Article.site_id == site.id)).all()
    }
    suggestions = [
        Suggestion(
            site_id=site.id,
            source_article_id=articles["1"].id,
            target_article_id=articles["2"].id,
            method="baseline_cosine",
            score=0.9,
            status=status,
        )
        for status in ("pending", "approved", "applying", "applied")
    ]
    db.add_all(suggestions)
    db.commit()

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: ReducedStubConnector(s)
    )
    ingestion_service.run_ingestion(site.id)

    db.expire_all()
    statuses = {suggestion.id: db.get(Suggestion, suggestion.id).status for suggestion in suggestions}
    assert [statuses[suggestion.id] for suggestion in suggestions] == [
        "expired",
        "expired",
        "applying",
        "applied",
    ]

    listed_ids = {
        item["id"] for item in client.get(f"/api/v1/suggestions/{site.id}").json()
    }
    assert suggestions[0].id not in listed_ids
    assert suggestions[1].id not in listed_ids
    review = client.put(
        f"/api/v1/suggestions/{suggestions[0].id}", json={"status": "approved"}
    )
    assert review.status_code == 409
    assert client.get(f"/api/v1/publish/{site.id}/status").json() == {
        "applied": 1,
        "awaiting_publication": 0,
    }


def test_failed_recrawl_preserves_previous_snapshot(db, site, monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_connector", lambda s: StubConnector(s))
    ingestion_service.run_ingestion(site.id)

    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda s: FailingStubConnector(s)
    )
    with pytest.raises(RuntimeError, match="crawl interrupted"):
        ingestion_service.run_ingestion(site.id)

    db.expire_all()
    articles = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    links = db.scalars(
        select(InternalLink)
        .join(Article, InternalLink.source_article_id == Article.id)
        .where(Article.site_id == site.id)
    ).all()
    membership_names = db.scalars(
        select(Taxonomy.name)
        .join(ArticleTaxonomy, ArticleTaxonomy.taxonomy_id == Taxonomy.id)
        .join(Article, ArticleTaxonomy.article_id == Article.id)
        .where(Article.site_id == site.id)
    ).all()
    runs = db.scalars(
        select(IngestionRun)
        .where(IngestionRun.site_id == site.id)
        .order_by(IngestionRun.id)
    ).all()
    previous_articles = [article for article in articles if article.external_id in {"1", "2"}]
    partial_articles = [article for article in articles if article.external_id not in {"1", "2"}]
    article_a = next(article for article in previous_articles if article.external_id == "1")

    assert all(article.is_active is True for article in previous_articles)
    assert article_a.title == "Article A"
    assert article_a.content_text == "text a"
    assert article_a.last_seen_run_id == runs[0].id
    assert partial_articles == []
    assert all(link.is_active is True for link in links)
    assert set(membership_names) == {"SEO"}
    assert [run.status for run in runs] == ["succeeded", "failed"]


def test_permalink_change_updates_in_place(db, site):
    """Same WP post id under a new URL must update the row, not violate (site_id, external_id)."""
    old = ArticleData(url=f"{site.base_url}/old-slug", title="T", content_text="x", external_id="99")
    new = ArticleData(url=f"{site.base_url}/new-slug", title="T", content_text="x", external_id="99")

    first_id = ingestion_service._upsert_article(db, site.id, old)
    db.commit()
    second_id = ingestion_service._upsert_article(db, site.id, new)
    db.commit()

    assert first_id == second_id
    assert db.get(Article, first_id).url == f"{site.base_url}/new-slug"


def test_reassigned_permalink_updates_existing_url_row(db, site):
    """A new WP post id at an existing permalink must replace the stale identity."""
    old = ArticleData(url=f"{site.base_url}/same-slug", title="Old", content_text="old", external_id="1")
    replacement = ArticleData(
        url=f"{site.base_url}/same-slug",
        title="Replacement",
        content_text="new",
        external_id="2",
    )

    first_id = ingestion_service._upsert_article(db, site.id, old)
    db.commit()
    replacement_id = ingestion_service._upsert_article(db, site.id, replacement)
    db.commit()

    assert replacement_id == first_id
    article = db.get(Article, first_id)
    assert article.external_id == "2"
    assert article.title == "Replacement"


def test_wordpress_ingestion_claims_existing_html_url_row(db, site):
    """Switching a site from HTML to WordPress must attach the WP id to its URL row."""
    html_article = ArticleData(
        url=f"{site.base_url}/shared", title="HTML", content_text="html", external_id=None
    )
    wordpress_article = ArticleData(
        url=f"{site.base_url}/shared", title="WordPress", content_text="wp", external_id="42"
    )

    html_id = ingestion_service._upsert_article(db, site.id, html_article)
    db.commit()
    wordpress_id = ingestion_service._upsert_article(db, site.id, wordpress_article)
    db.commit()

    assert wordpress_id == html_id
    article = db.get(Article, html_id)
    assert article.external_id == "42"
    assert article.title == "WordPress"


def test_slug_swap_preserves_both_article_identities(db, site):
    """A WP post moving onto another post's permalink must not violate URL uniqueness."""
    first = ArticleData(
        url=f"{site.base_url}/first", title="First", content_text="first", external_id="1"
    )
    second = ArticleData(
        url=f"{site.base_url}/second", title="Second", content_text="second", external_id="2"
    )
    moved_first = ArticleData(
        url=f"{site.base_url}/second", title="First moved", content_text="moved", external_id="1"
    )

    first_id = ingestion_service._upsert_article(db, site.id, first)
    second_id = ingestion_service._upsert_article(db, site.id, second)
    db.commit()
    moved_id = ingestion_service._upsert_article(db, site.id, moved_first)
    db.commit()

    assert moved_id == first_id
    assert db.get(Article, first_id).url == f"{site.base_url}/second"
    assert db.get(Article, second_id).url == f"{site.base_url}/first"


def test_url_only_ingestion_preserves_wordpress_external_id(db, site):
    wordpress_article = ArticleData(
        url=f"{site.base_url}/shared", title="WordPress", content_text="wp", external_id="42"
    )
    url_only_article = ArticleData(
        url=f"{site.base_url}/shared", title="HTML", content_text="html", external_id=None
    )

    article_id = ingestion_service._upsert_article(db, site.id, wordpress_article)
    db.commit()
    same_id = ingestion_service._upsert_article(db, site.id, url_only_article)
    db.commit()

    assert same_id == article_id
    assert db.get(Article, article_id).external_id == "42"


def test_concurrent_article_insert_resolves_to_one_row(site):
    article = ArticleData(
        url=f"{site.base_url}/concurrent", title="Concurrent", content_text="content", external_id="7"
    )
    lookups = Barrier(2, timeout=1)

    def synchronize_empty_lookups(conn, cursor, statement, parameters, context, executemany):
        if "FROM articles" in statement and "FOR UPDATE" in statement:
            try:
                lookups.wait()
            except BrokenBarrierError:
                pass

    def upsert_in_separate_transaction():
        with SessionLocal() as session:
            article_id = ingestion_service._upsert_article(session, site.id, article)
            session.commit()
            return article_id

    event.listen(engine, "after_cursor_execute", synchronize_empty_lookups)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            article_ids = list(executor.map(lambda _: upsert_in_separate_transaction(), range(2)))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_empty_lookups)

    assert article_ids[0] == article_ids[1]


def test_failed_run_never_stays_running(db, site, monkeypatch):
    def boom(s):
        raise RuntimeError("connector exploded")

    monkeypatch.setattr(ingestion_service, "get_connector", boom)
    try:
        ingestion_service.run_ingestion(site.id)
    except RuntimeError:
        pass
    run = db.scalars(select(IngestionRun).where(IngestionRun.site_id == site.id)).one()
    assert run.status == "failed"
    assert "connector exploded" in run.error
