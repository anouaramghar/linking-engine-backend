"""Ingestion pipeline against a stub connector — no network, real database."""

from sqlalchemy import select

import app.services.ingestion_service as ingestion_service
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
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
