"""Focused regression coverage for auth, remote-work, and analysis budgets."""

import gzip
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings, settings
from app.connectors.base import ArticleData, ContentConnector
from app.connectors.html_crawler import HTMLConnector
from app.connectors import http_limits
from app.connectors.http_limits import ResponseTooLargeError, get_limited_http_response
from app.connectors.wordpress import WordPressConnector
from app.ml.hybrid import CorpusArticle, structured_terms
from app.models import Article, IngestionRun
from app.services import ingestion_service, suggestion_service


def _site(base_url: str = "https://example.com") -> SimpleNamespace:
    return SimpleNamespace(
        name="security-test",
        base_url=base_url,
        wp_username=None,
        wp_app_password=None,
        platform="wordpress",
    )


def test_html_sitemap_response_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "crawl_max_response_bytes", 16)
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", True)
    connector = HTMLConnector(_site("http://127.0.0.1:8080"))
    connector.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 32, request=request)
        )
    )

    with pytest.raises(ResponseTooLargeError, match="response declares|decoded-body limit"):
        connector._sitemap_urls()


def test_bounded_http_response_does_not_decode_compressed_body_twice():
    payload = b'{"name":"vibe"}'

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(payload),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = get_limited_http_response(
        client, "https://example.com/wp-json/wp/v2/sites", max_bytes=1024
    )

    assert response.content == payload
    assert "content-encoding" not in response.headers


def test_streaming_response_rechecks_the_crawl_deadline_for_each_chunk(monkeypatch):
    checked = []
    monkeypatch.setattr(http_limits, "check_crawl_deadline", checked.append)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"chunk", request=request)
        )
    )

    response = get_limited_http_response(
        client,
        "https://example.com/data",
        max_bytes=100,
        crawl_started_at=123.0,
    )

    assert response.content == b"chunk"
    assert checked == [123.0]


def test_wordpress_pagination_has_a_local_page_budget(monkeypatch):
    monkeypatch.setattr(settings, "crawl_max_wordpress_pages", 2)
    connector = WordPressConnector(_site())
    connector._api_candidates = ["https://example.com/wp-json/wp/v2/"]
    requested_pages = []

    def handler(request):
        requested_pages.append(int(request.url.params["page"]))
        return httpx.Response(200, json=[{"id": requested_pages[-1]}], request=request)

    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="page limit"):
        list(connector._paginate("posts"))
    assert requested_pages == [1, 2]


def test_default_wordpress_page_budget_supports_large_sites_without_being_unbounded():
    assert Settings.model_fields["crawl_max_wordpress_pages"].default == 1_000


def test_wordpress_article_content_has_a_character_budget(monkeypatch):
    monkeypatch.setattr(settings, "crawl_max_article_chars", 3)
    connector = WordPressConnector(_site())

    with pytest.raises(ValueError, match="characters"):
        connector._to_article(
            {
                "id": 1,
                "link": "https://example.com/post",
                "title": {"rendered": "Post"},
                "content": {"rendered": "<p>four</p>"},
                "categories": [],
                "tags": [],
                "date_gmt": None,
            },
            {},
        )


class _TooManyArticlesConnector(ContentConnector):
    def fetch_articles(self):
        for index in range(2):
            yield ArticleData(
                url=f"{self.site.base_url}/article-{index}",
                title=f"Article {index}",
                content_text="content",
            )

    def fetch_article_by_url(self, url):
        return None

    def get_site_metadata(self):
        raise NotImplementedError

    def supports_incremental_sync(self):
        return False

    def apply_planned_edit(self, source, *, original_html, updated_html):
        raise NotImplementedError


def test_ingestion_rejects_articles_above_the_crawl_budget(db, site, monkeypatch):
    monkeypatch.setattr(settings, "crawl_max_articles", 1)
    monkeypatch.setattr(
        ingestion_service,
        "get_connector",
        lambda current_site: _TooManyArticlesConnector(current_site),
    )

    with pytest.raises(ValueError, match="article count"):
        ingestion_service.run_ingestion(site.id)

    run = db.query(IngestionRun).filter(IngestionRun.site_id == site.id).one()
    assert run.status == "failed"
    assert db.query(Article).filter(Article.site_id == site.id).count() == 0


def test_embedding_rejects_an_oversized_site_before_encoding(db, site, monkeypatch):
    monkeypatch.setattr(settings, "analysis_max_articles_per_site", 1)
    db.add_all(
        [
            Article(
                site_id=site.id,
                url=f"{site.base_url}/article-{index}",
                title=f"Article {index}",
                content_text="content",
                is_active=True,
            )
            for index in range(2)
        ]
    )
    db.commit()

    with pytest.raises(ValueError, match="analysis article count"):
        suggestion_service._embed_missing(db, site.id, settings.embedding_model)


def test_hybrid_tokenization_rejects_oversized_article(monkeypatch):
    monkeypatch.setattr(settings, "crawl_max_article_chars", 3)
    article = CorpusArticle(
        id=1,
        title="Title",
        content_text="four",
        content_fingerprint=None,
        taxonomy_names=(),
    )

    with pytest.raises(ValueError, match="characters"):
        structured_terms(article)
