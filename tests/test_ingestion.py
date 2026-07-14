"""Ingestion pipeline against a stub connector — no network, real database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

from sqlalchemy import event, select

import app.services.ingestion_service as ingestion_service
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
from app.db import SessionLocal, engine
from app.models import Article, IngestionRun, InternalLink, Taxonomy


class StubConnector(ContentConnector):
    def fetch_articles(self):
        base = self.site.base_url
        yield ArticleData(
            url=f"{base}/a",
            title="Article A",
            content_text="text a",
            external_id="1",
            taxonomies=[TaxonomyData(kind="category", name="SEO")],
            outbound_internal_urls=[f"{base}/b", f"{base}/b#frag", f"{base}/missing"],
        )
        yield ArticleData(
            url=f"{base}/b/",
            title="Article B",
            content_text="text b",
            external_id="2",
            outbound_internal_urls=[f"{base}/a"],
        )

    def fetch_article_by_url(self, url):
        return None

    def get_site_metadata(self):
        return SiteMetadata(name="stub", base_url=self.site.base_url, platform="wordpress")

    def supports_incremental_sync(self):
        return False

    def apply_link(self, suggestion):
        pass


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
