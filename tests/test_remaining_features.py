"""Contract tests for the remaining v2 implementation slices."""

from types import SimpleNamespace

import httpx
from sqlalchemy import select

import app.services.ingestion_service as ingestion_service
from app.config import settings
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    DiscoveryObservation,
    SiteMetadata,
)
from app.connectors.html_crawler import HTMLConnector
from app.models import (
    Article,
    IngestionDiagnostic,
    IngestionRun,
    JobRun,
    PublicationPlan,
    Suggestion,
)
from app.services.publication_plan_service import compute_plan_hash
from app.tasks.queues import redis_conn


def _html_site(base_url: str = "http://127.0.0.1:8080") -> SimpleNamespace:
    return SimpleNamespace(name="html-test", base_url=base_url, platform="html")


def test_html_crawl_falls_back_to_bounded_bfs_and_keeps_reasons(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", True)
    monkeypatch.setattr(settings, "crawl_bfs_fallback_enabled", True)
    monkeypatch.setattr(settings, "crawl_max_depth", 1)
    monkeypatch.setattr(settings, "crawl_max_discovered_urls", 10)
    monkeypatch.setattr(
        "app.connectors.html_crawler.trafilatura.bare_extraction",
        lambda text, **_: (
            None
            if "<article>" not in text
            else SimpleNamespace(title="Article A", text="Useful content.")
        ),
    )

    pages = {
        "/": b'<html><body><a href="/article-a?utm_source=test#top">Article A</a>'
        b'<a href="https://evil.example/out">outside</a></body></html>',
        "/article-a": b"<html><body><article><h1>Article A</h1><p>Useful content.</p></article></body></html>",
    }

    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.url.path == "/sitemap_index.xml":
            return httpx.Response(404, request=request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=pages.get(request.url.path, b""), request=request)

    connector = HTMLConnector(_html_site())
    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    articles = list(connector.fetch_articles())
    observations = connector.drain_discovery_observations()

    assert [article.url for article in articles] == ["http://127.0.0.1:8080/article-a"]
    assert any(
        item.state == "skipped" and item.reason_code == "sitemap_unavailable"
        for item in observations
    )
    accepted = next(item for item in observations if item.state == "accepted")
    assert accepted.url == "http://127.0.0.1:8080/article-a"
    assert accepted.discovered_from == "http://127.0.0.1:8080/"
    assert accepted.depth == 1
    assert not any("evil.example" in url for url in fetched)


def test_html_crawl_records_noindex_without_ingesting_it(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", True)
    monkeypatch.setattr(settings, "crawl_bfs_fallback_enabled", True)
    monkeypatch.setattr(settings, "crawl_max_depth", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404, request=request)
        if request.url.path == "/":
            return httpx.Response(
                200,
                content=b'<a href="/private">private</a>',
                request=request,
            )
        return httpx.Response(
            200,
            content=b'<meta name="robots" content="noindex"><article><p>hidden</p></article>',
            request=request,
        )

    connector = HTMLConnector(_html_site())
    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert list(connector.fetch_articles()) == []
    assert any(
        item.url.endswith("/private") and item.state == "skipped" and item.reason_code == "noindex"
        for item in connector.drain_discovery_observations()
    )


def test_html_sitemap_crawl_honors_robots_rules(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap_index.xml":
            return httpx.Response(
                200,
                content=(
                    "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    "<url><loc>http://127.0.0.1:8080/article</loc></url></urlset>"
                ).encode(),
                request=request,
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /article\n",
                request=request,
            )
        raise AssertionError(f"robots-denied URL was fetched: {request.url}")

    connector = HTMLConnector(_html_site())
    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert list(connector.fetch_articles()) == []
    assert any(
        item.reason_code == "robots_denied" and item.state == "skipped"
        for item in connector.drain_discovery_observations()
    )


def test_static_html_preview_produces_an_exact_read_only_artifact():
    source = SimpleNamespace(
        id=1,
        url="https://example.com/source",
        content_html="<article>Read this useful phrase.</article>",
    )

    class FakeSuggestion:
        source_article = source
        anchor_text = "useful phrase"
        resolved_target_url = "https://example.com/target"
        resolved_target_title = "Target"

    connector = HTMLConnector(_html_site("https://example.com"))
    preview = connector.preview_links([FakeSuggestion()])

    assert preview.original_content == source.content_html
    assert preview.outcomes == ["inserted"]
    assert '<a href="https://example.com/target">useful phrase</a>' in preview.updated_content


class DiagnosticConnector(ContentConnector):
    def fetch_articles(self):
        self.record_discovery(
            DiscoveryObservation(
                url=f"{self.site.base_url}/article",
                state="accepted",
                reason_code="accepted",
                discovered_from=self.site.base_url,
                depth=1,
            )
        )
        yield ArticleData(
            url=f"{self.site.base_url}/article",
            title="Article",
            content_text="content",
        )

    def fetch_article_by_url(self, url):
        return None

    def get_site_metadata(self):
        return SiteMetadata(
            name=self.site.name, base_url=self.site.base_url, platform=self.site.platform
        )

    def supports_incremental_sync(self):
        return False

    def apply_planned_edit(self, source, *, original_html, updated_html):
        raise NotImplementedError


def test_ingestion_persists_connector_diagnostics(db, site, monkeypatch):
    monkeypatch.setattr(
        ingestion_service, "get_connector", lambda current: DiagnosticConnector(current)
    )

    result = ingestion_service.run_ingestion(site.id)

    assert result["articles"] == 1
    run = db.scalars(
        select(IngestionRun).where(IngestionRun.site_id == site.id).order_by(IngestionRun.id.desc())
    ).first()
    diagnostic = db.scalars(
        select(IngestionDiagnostic).where(IngestionDiagnostic.ingestion_run_id == run.id)
    ).one()
    assert run.discovered_urls == 1
    assert run.accepted_urls == 1
    assert diagnostic.reason_code == "accepted"


def test_article_import_accepts_screaming_frog_aliases_and_keeps_row_reasons(client, db, site):
    response = client.post(
        f"/api/v1/sites/{site.id}/articles/import",
        json={
            "rows": [
                {
                    "Address": f"{site.base_url}/imported?utm_source=crawl",
                    "Title 1": "Imported article",
                    "Content": "Imported article text",
                    "Status Code": "200",
                    "Indexability Status": "Indexable",
                },
                {
                    "Address": f"{site.base_url}/private",
                    "Title 1": "Private",
                    "Indexability Status": "Non-Indexable",
                },
                {
                    "Address": "https://other.example/outside",
                    "Title 1": "Outside",
                },
            ]
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["imported"], len(body["skipped"]), len(body["rejected"])) == (1, 1, 1)
    assert body["skipped"][0]["reason"] == "indexability is Non-Indexable"
    assert "not on host" in body["rejected"][0]["reason"]
    article = db.scalar(select(Article).where(Article.site_id == site.id))
    assert article.url == f"{site.base_url}/imported"
    assert article.is_active is True
    assert body["diagnostic_summary"]["accepted"] == 1


def test_article_generation_trigger_scopes_the_existing_task(client, db, site):
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="Source",
        content_text="source content",
    )
    db.add(article)
    db.commit()

    response = client.post(f"/api/v1/articles/{article.id}/suggestions")

    assert response.status_code == 202, response.text
    body = response.json()
    run = db.get(JobRun, body["job_run_id"])
    assert run.kind == "analysis"
    job = __import__("rq.job", fromlist=["Job"]).Job.fetch(body["job_id"], connection=redis_conn)
    assert job.func_name == "app.tasks.analysis.analyze_article"
    assert job.kwargs["article_id"] == article.id
    job.delete()


def test_non_wordpress_export_streams_only_verified_approved_plans(client, db, site):
    site.platform = "html"
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="Source",
        content_text="source content",
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/target",
        title="Target",
        content_text="target content",
    )
    db.add_all([source, target])
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=0.9,
        rank_score=0.9,
        status="approved",
        anchor_text="target",
        placement_context="source context",
    )
    db.add(suggestion)
    db.flush()
    plan = PublicationPlan(
        site_id=site.id,
        source_article_id=source.id,
        source_url=source.url,
        status="approved",
        original_html="<p>source</p>",
        updated_html='<p>source <a href="/target">target</a></p>',
        items=[
            {
                "position": 0,
                "suggestion_id": suggestion.id,
                "target_url": target.url,
                "anchor_text": "target",
                "outcome": "inserted",
            }
        ],
        plan_hash="pending",
        approved_hash=None,
        approved_by="operator:test",
    )
    db.add(plan)
    db.flush()
    plan.plan_hash = compute_plan_hash(plan)
    plan.approved_hash = plan.plan_hash
    db.commit()

    response = client.get(f"/api/v1/publish/{site.id}/export.csv")

    assert response.status_code == 200, response.text
    assert "plan_id,plan_hash,site_id" in response.text
    assert str(plan.id) in response.text
    assert source.url in response.text
    assert target.url in response.text
