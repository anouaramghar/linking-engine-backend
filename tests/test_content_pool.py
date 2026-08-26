"""Content pools are read-only cross-site targets, never suggestion sources."""

import hashlib
import json
import uuid
from types import SimpleNamespace

import feedparser
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.api.deps import require_api_key
from app.config import settings
from app.connectors.feed_discovery import (
    FeedNotFoundError,
    FeedPayloadError,
    validate_feed_payload,
)
from app.connectors.registry import get_connector
from app.connectors.rss_connector import RSSConnector
from app.connectors.url_guard import UnsafeURLError
from app.connectors.wikipedia_connector import WikipediaConnector
from app.main import app
from app.ml.external.cleaning import (
    deduplicate_external_urls,
    normalize_external_url,
)
from app.models import (
    Article,
    Embedding,
    ExternalLinkPolicy,
    IngestionRun,
    PoolSourceAuditEvent,
    Site,
    Suggestion,
)
from app.models.article import EMBEDDING_DIM
from app.schemas.site import SiteCreate
from app.services.crawl_snapshot import _reconcile_snapshot
from app.services.live_url import LiveURLChecker
from app.services.pool_source_policy import (
    PoolSourceFetchError,
    PoolSourcePolicyError,
    PoolSourceQuarantinedError,
    is_pool_domain_allowed,
    require_approved_pool_source,
)
from app.services.suggestion_service import generate_suggestions
from app.tasks import ingestion as ingestion_task
from app.tasks import pool_ingestion


def _passing_live_url_checker() -> LiveURLChecker:
    return LiveURLChecker(
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        validator=lambda _url: None,
    )


def _vector(first: float, second: float = 0.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0], vector[1] = first, second
    return vector


def _fingerprint(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()


def _pool(db, *, frequency: str = "daily", approved: bool = True) -> Site:
    site = Site(
        name="News pool",
        base_url=f"https://pool-{uuid.uuid4().hex[:8]}.wikipedia.org/feed.xml",
        platform="pool",
        crawl_frequency=frequency,
        pool_source_approved=approved,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _delete_audit_events(db, site_id: int) -> None:
    db.execute(delete(PoolSourceAuditEvent).where(PoolSourceAuditEvent.site_id == site_id))
    db.commit()


def test_pool_schema_defaults_to_daily_and_rejects_credentials(monkeypatch):
    # `SiteCreate` relaxes the HTTPS and private-address rules when this is on,
    # and settings fall back to the developer's .env, which the suite shares.
    # Pinned here so this asserts the guard rather than the machine it runs on,
    # as `test_url_guard` and `test_site_bulk_import` already do.
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", False)

    payload = SiteCreate(
        name="Wikipedia",
        base_url="https://en.wikipedia.org/wiki/Search_engine_optimization",
        platform="pool",
    )
    assert payload.crawl_frequency == "daily"

    with pytest.raises(ValueError, match="only valid for WordPress"):
        SiteCreate(
            name="Feed",
            base_url="https://example.com/feed.xml",
            platform="pool",
            wp_username="editor",
            wp_app_password="secret",
        )

    with pytest.raises(ValueError, match="reserved for content-pool"):
        SiteCreate(
            name="Docs",
            base_url="https://example.com/docs",
            platform="html",
            crawl_frequency="daily",
        )

    with pytest.raises(ValueError, match="HTTPS required"):
        SiteCreate(
            name="Insecure pool",
            base_url="http://en.wikipedia.org/wiki/Search_engine_optimization",
            platform="pool",
        )


def test_external_urls_have_one_stable_storage_identity():
    assert (
        normalize_external_url(
            " HTTPS://B\u00dcCHER.Example:443/report/?utm_source=test&id=7&b=2#section "
        )
        == "https://xn--bcher-kva.example/report/?b=2&id=7"
    )
    assert normalize_external_url("http://[2001:DB8::1]:80") == "http://[2001:db8::1]/"

    assert deduplicate_external_urls(
        [
            "https://Example.com/report?id=7&utm_medium=email",
            "https://example.com:443/report?utm_source=search&id=7#result",
            "https://example.com/report?id=8",
        ]
    ) == [
        "https://example.com/report?id=7",
        "https://example.com/report?id=8",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://example.com/report",
        "https://user:secret@example.com/report",
        "https://example.com/a path",
        "https://example.com:99999/report",
    ],
)
def test_external_url_normalization_rejects_unsafe_or_ambiguous_values(value):
    with pytest.raises(ValueError, match="external URL"):
        normalize_external_url(value)


def test_pool_registration_normalizes_the_source_url_before_deduplication():
    payload = SiteCreate(
        name="Tracked feed",
        base_url="HTTPS://EN.WIKIPEDIA.ORG:443/feed.xml?utm_source=setup&id=7#feed",
        platform="pool",
    )

    assert payload.base_url == "https://en.wikipedia.org/feed.xml?id=7"


def test_pool_allowlist_accepts_subdomains_but_not_lookalikes(monkeypatch):
    monkeypatch.setattr(settings, "pool_allowed_domains", "wikipedia.org,publisher.example")

    assert is_pool_domain_allowed("https://en.wikipedia.org/wiki/Link_analysis")
    assert is_pool_domain_allowed("https://news.publisher.example/feed.xml")
    assert not is_pool_domain_allowed("https://evilwikipedia.org/feed.xml")


def test_registry_selects_rss_or_wikipedia_connector():
    rss = Site(
        name="RSS",
        base_url="https://feeds.wikipedia.org/feed.xml",
        platform="pool",
        pool_source_approved=True,
    )
    wiki = Site(
        name="Wiki",
        base_url="https://en.wikipedia.org/wiki/Link_analysis",
        platform="pool",
        pool_source_approved=True,
    )
    assert isinstance(get_connector(rss), RSSConnector)
    assert isinstance(get_connector(wiki), WikipediaConnector)


def test_rss_entry_is_normalized_without_fetching_the_article_page():
    site = Site(name="RSS", base_url="https://feeds.wikipedia.org/feed.xml", platform="pool")
    connector = RSSConnector(site)
    parsed = feedparser.parse(
        b"""<?xml version='1.0'?><rss version='2.0'><channel><title>News</title>
        <item><guid>item-1</guid><title>Useful &amp; safe</title>
        <link>https://example.com/useful</link>
        <description><![CDATA[<p>Hello <b>world</b>.</p>]]></description>
        </item></channel></rss>"""
    )
    article = connector._to_article(parsed.entries[0], "en")
    connector.client.close()

    assert article is not None
    assert article.title == "Useful & safe"
    assert article.content_text == "Hello world ."
    assert article.external_id == "item-1"


def test_rss_rejects_html_and_oversized_responses(monkeypatch):
    site = Site(name="RSS", base_url="https://feeds.wikipedia.org/feed.xml", platform="pool")
    html_transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            content=b"<html>not a feed</html>",
        )
    )
    # A page where a feed was expected starts a search for the real feed; this
    # host serves the same page everywhere, so the source still fails. See
    # tests/test_feed_discovery.py for the search itself.
    with pytest.raises(FeedNotFoundError, match="no feed was found"):
        RSSConnector(site, transport=html_transport)._feed()
    with pytest.raises(FeedPayloadError, match="HTML page"):
        validate_feed_payload(b"<html>not a feed</html>", "application/rss+xml")

    monkeypatch.setattr(settings, "pool_max_response_bytes", 8)
    large_transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            stream=httpx.ByteStream(b"x" * 9),
        )
    )
    with pytest.raises(ValueError, match="decoded-body limit"):
        RSSConnector(site, transport=large_transport)._feed()


def test_rss_rejects_malformed_and_binary_responses():
    site = Site(name="RSS", base_url="https://feeds.wikipedia.org/feed.xml", platform="pool")
    malformed = RSSConnector(
        site,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"<rss><broken>")
        ),
    )
    binary = RSSConnector(
        site,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"\x00\x01not-a-feed")
        ),
    )
    try:
        with pytest.raises(ValueError, match="invalid RSS/Atom feed"):
            list(malformed.fetch_articles())
        with pytest.raises(ValueError, match="non-XML or binary response"):
            list(binary.fetch_articles())
    finally:
        malformed.client.close()
        binary.client.close()


def test_rss_rejects_non_feed_content_type():
    connector = RSSConnector(
        Site(name="RSS", base_url="https://feeds.wikipedia.org/feed.xml", platform="pool"),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html>temporary error</html>",
            )
        ),
    )
    try:
        # An HTML error page is never accepted as content, whether the search for
        # a real feed runs or not.
        with pytest.raises(FeedNotFoundError, match="no feed was found"):
            list(connector.fetch_articles())
        with pytest.raises(FeedPayloadError, match="unsupported Content-Type 'text/html'"):
            validate_feed_payload(b"<html>temporary error</html>", "text/html")
    finally:
        connector.client.close()


def test_pool_connector_bounds_stored_title_and_content(monkeypatch):
    monkeypatch.setattr(settings, "pool_max_title_chars", 12)
    monkeypatch.setattr(settings, "pool_max_article_chars", 20)
    feed = b"""<rss version="2.0"><channel><title>Feed</title><item>
    <guid>bounded</guid><title>A title that is much too long</title>
    <link>https://example.com/bounded</link>
    <description>A description that is much too long for storage.</description>
    </item></channel></rss>"""
    connector = RSSConnector(
        Site(name="RSS", base_url="https://feeds.wikipedia.org/feed.xml", platform="pool"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=feed)),
    )
    try:
        article = next(connector.fetch_articles())
    finally:
        connector.client.close()

    assert article.title == "A title that"
    assert len(article.content_text) <= 20
    assert len(article.content_html or "") <= 20


def test_pool_connectors_reject_redirects_outside_allowlist():
    requests: list[str] = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://outside.example/feed.xml"},
        )

    rss = RSSConnector(
        Site(
            name="RSS",
            base_url="https://feeds.wikipedia.org/feed.xml",
            platform="pool",
        ),
        transport=httpx.MockTransport(handler),
    )
    wiki = WikipediaConnector(
        Site(
            name="Wiki",
            base_url="https://en.wikipedia.org/wiki/Link_analysis",
            platform="pool",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PoolSourcePolicyError, match="outside.example"):
            rss._feed()
        with pytest.raises(PoolSourcePolicyError, match="outside.example"):
            wiki._pages(titles="Link analysis")
    finally:
        rss.client.close()
        wiki.client.close()

    assert all("outside.example" not in url for url in requests)


def test_wikipedia_connector_follows_continuation_and_validates_json(monkeypatch):
    site = Site(
        name="Wiki",
        base_url="https://en.wikipedia.org/wiki/Link_analysis",
        platform="pool",
    )
    pages = [
        {
            "pageid": 1,
            "title": "First",
            "fullurl": "https://en.wikipedia.org/wiki/First",
            "extract": "first article",
            "revisions": [{"timestamp": "2026-08-01T00:00:00Z"}],
        },
        {
            "pageid": 2,
            "title": "Second",
            "fullurl": "https://en.wikipedia.org/wiki/Second",
            "extract": "second article",
            "revisions": [{"timestamp": "2026-08-01T00:00:00Z"}],
        },
    ]
    calls = 0
    sleeps: list[float] = []

    def handler(request):
        nonlocal calls
        calls += 1
        assert "rvlimit" not in request.url.params
        payload = {"query": {"pages": [pages[calls - 1]]}}
        if calls == 1:
            payload["continue"] = {"continue": "-||", "gsroffset": 20}
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            content=json.dumps(payload).encode(),
        )

    monkeypatch.setattr(settings, "pool_max_articles_per_source", 2)
    monkeypatch.setattr(
        "app.connectors.wikipedia_connector.time.sleep", lambda delay: sleeps.append(delay)
    )
    connector = WikipediaConnector(site, transport=httpx.MockTransport(handler))
    articles = list(connector.fetch_articles())

    assert [article.title for article in articles] == ["First", "Second"]
    assert calls == 2
    assert sleeps == [settings.pool_request_delay_seconds]


def test_wikipedia_direct_article_lookup_ignores_older_revision_continuation():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert request.url.params["rvlimit"] == "1"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "query": {
                        "pages": [
                            {
                                "pageid": 1,
                                "title": "RSS",
                                "fullurl": "https://en.wikipedia.org/wiki/RSS",
                                "extract": "RSS is a web feed format.",
                                "revisions": [{"timestamp": "2026-08-01T00:00:00Z"}],
                            }
                        ]
                    },
                    "continue": {"rvcontinue": "older-revision", "continue": "-||"},
                }
            ).encode(),
        )

    connector = WikipediaConnector(
        Site(
            name="Wiki",
            base_url="https://en.wikipedia.org/wiki/RSS",
            platform="pool",
        ),
        transport=httpx.MockTransport(handler),
    )

    article = connector.fetch_article_by_url("https://en.wikipedia.org/wiki/RSS")

    connector.client.close()
    assert article is not None
    assert article.title == "RSS"
    assert calls == 1


def test_wikipedia_connector_bounds_empty_continuations(monkeypatch):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"query": {"pages": []}, "continue": {"gsroffset": calls}}).encode(),
        )

    monkeypatch.setattr(settings, "pool_max_articles_per_source", 2)
    monkeypatch.setattr("app.connectors.wikipedia_connector.time.sleep", lambda _delay: None)
    connector = WikipediaConnector(
        Site(
            name="Wiki",
            base_url="https://en.wikipedia.org/wiki/Link_analysis",
            platform="pool",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="pagination exceeded request limit"):
        list(connector.fetch_articles())

    connector.client.close()
    assert calls == 2


def test_wikipedia_connector_rejects_oversized_and_non_json_responses(monkeypatch):
    monkeypatch.setattr(settings, "pool_max_response_bytes", 1024)
    site = Site(
        name="Wiki",
        base_url="https://en.wikipedia.org/wiki/Link_analysis",
        platform="pool",
    )
    oversized = WikipediaConnector(
        site,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                stream=httpx.ByteStream(b"x" * 1025),
            )
        ),
    )
    non_json = WikipediaConnector(
        site,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html>temporary error</html>",
            )
        ),
    )
    try:
        with pytest.raises(ValueError, match="decoded-body limit"):
            list(oversized.fetch_articles())
        with pytest.raises(ValueError, match="unsupported Content-Type text/html"):
            list(non_json.fetch_articles())
    finally:
        oversized.client.close()
        non_json.client.close()


def test_pool_connector_rejects_http_for_existing_rows(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", False)
    with pytest.raises(UnsafeURLError, match="HTTPS required"):
        RSSConnector(
            Site(name="RSS", base_url="http://feeds.wikipedia.org/feed.xml", platform="pool"),
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        )


def test_pool_connector_allows_http_with_the_unsafe_switch(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", True)
    monkeypatch.setattr(settings, "pool_allowed_domains", "127.0.0.1")
    connector = RSSConnector(
        Site(name="Local RSS", base_url="http://127.0.0.1:8090/feed.xml", platform="pool"),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"<rss version='2.0'><channel /></rss>",
            )
        ),
    )
    try:
        connector._feed()
    finally:
        connector.client.close()


def test_pool_task_records_source_failures_only(monkeypatch):
    recorded: list[tuple[int, Exception]] = []
    monkeypatch.setattr(ingestion_task, "_is_terminal_attempt", lambda: True)
    monkeypatch.setattr(
        ingestion_task,
        "_record_pool_ingestion_failure",
        lambda site_id, error: recorded.append((site_id, error)),
    )

    def source_failure(*_args, **_kwargs):
        raise PoolSourceFetchError("feed down")

    monkeypatch.setattr(ingestion_task, "run_durably", source_failure)
    with pytest.raises(PoolSourceFetchError):
        ingestion_task.ingest_pool_site(7)
    assert [site_id for site_id, _error in recorded] == [7]

    recorded.clear()
    monkeypatch.setattr(ingestion_task, "_is_terminal_attempt", lambda: False)
    with pytest.raises(PoolSourceFetchError):
        ingestion_task.ingest_pool_site(7)
    assert recorded == []

    def internal_failure(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ingestion_task, "run_durably", internal_failure)
    with pytest.raises(RuntimeError):
        ingestion_task.ingest_pool_site(7)
    assert recorded == []


def test_pool_task_rejects_non_pool_sites_before_ingestion(monkeypatch):
    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args):
            return SimpleNamespace(id=7, platform="wordpress")

    monkeypatch.setattr(ingestion_task, "SessionLocal", lambda: FakeDB())

    with pytest.raises(PoolSourcePolicyError, match="not a content-pool source"):
        ingestion_task._run_pool_ingestion(7)


def test_pool_success_bookkeeping_runs_inside_durable_body(monkeypatch):
    events: list[str] = []

    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args):
            return SimpleNamespace(platform="pool")

    monkeypatch.setattr(ingestion_task, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(ingestion_task, "require_approved_pool_source", lambda _site: None)
    monkeypatch.setattr(
        ingestion_task,
        "_record_pool_ingestion_success",
        lambda _site_id: events.append("pool-success"),
    )

    def fake_run_ingestion(_site_id, job_run_id=None):
        events.append(f"ingestion:{job_run_id}")
        return {"articles": 1}

    monkeypatch.setattr(ingestion_task, "run_ingestion", fake_run_ingestion)

    def fake_run_durably(job_run_id, fn, site_id):
        events.append("durable-start")
        result = fn(site_id, job_run_id=job_run_id)
        events.append("durable-return")
        return result

    monkeypatch.setattr(ingestion_task, "run_durably", fake_run_durably)

    assert ingestion_task.ingest_pool_site(7, 9) == {"articles": 1}
    assert events == ["durable-start", "ingestion:9", "pool-success", "durable-return"]


def test_pool_source_must_be_approved_before_ingestion_and_can_be_revoked(client, db):
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "Approved pool",
            "base_url": "https://en.wikipedia.org/wiki/Search_engine_optimization",
            "platform": "pool",
        },
    )
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    try:
        assert response.json()["pool_source_approved"] is False
        assert client.post(f"/api/v1/sites/{site_id}/ingest").status_code == 409

        approved = client.post(
            f"/api/v1/sites/{site_id}/pool-source/approval",
            json={"approved_by": "spoofed-editor"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["pool_source_approved"] is True
        assert approved.json()["pool_source_approved_by"] == "local-development"

        site = db.get(Site, site_id)
        site.pool_source_consecutive_failures = 3
        site.pool_source_quarantined = True
        site.pool_source_quarantine_reason = "temporary outage"
        db.commit()
        reactivated = client.post(
            f"/api/v1/sites/{site_id}/pool-source/reactivate",
            json={"reactivated_by": "spoofed-operator"},
        )
        assert reactivated.status_code == 200, reactivated.text
        assert reactivated.json()["pool_source_quarantined"] is False
        assert reactivated.json()["pool_source_consecutive_failures"] == 0
        assert reactivated.json()["pool_source_last_reactivated_by"] == "local-development"

        revoked = client.delete(f"/api/v1/sites/{site_id}/pool-source/approval")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["pool_source_approved"] is False
        assert client.post(f"/api/v1/sites/{site_id}/ingest").status_code == 409

        history = client.get(f"/api/v1/sites/{site_id}/pool-source/audit-events")
        assert history.status_code == 200
        assert [event["action"] for event in history.json()] == [
            "revoked",
            "reactivated",
            "approved",
        ]
        assert [event["operator_id"] for event in history.json()] == [
            "local-development",
            "local-development",
            "local-development",
        ]
    finally:
        site = db.get(Site, site_id)
        if site is not None:
            db.delete(site)
            db.commit()
        _delete_audit_events(db, site_id)


def test_a_managed_domain_can_never_become_a_pool_target(client, db, monkeypatch):
    """PBN rule: a domain we run for a client must not be a link target for other
    clients, whichever order the two sites are created in."""
    monkeypatch.setattr(settings, "pool_allowed_domains", "wikipedia.org,client.example")
    created: list[int] = []
    try:
        managed = client.post(
            "/api/v1/sites",
            json={
                "name": "Client blog",
                "base_url": "https://blog.client.example",
                "platform": "wordpress",
            },
        )
        assert managed.status_code == 201, managed.text
        created.append(managed.json()["id"])

        pool = client.post(
            "/api/v1/sites",
            json={
                "name": "Same property, as a pool source",
                "base_url": "https://client.example/feed.xml",
                "platform": "pool",
            },
        )
        assert pool.status_code == 201, pool.text
        pool_id = pool.json()["id"]
        created.append(pool_id)

        refused = client.post(f"/api/v1/sites/{pool_id}/pool-source/approval")
        assert refused.status_code == 409, refused.text
        assert "private blog network" in refused.text
        db.expire_all()
        assert db.get(Site, pool_id).pool_source_approved is False

        # Reverse order: the pool source is approved before the client site exists.
        wiki = client.post(
            "/api/v1/sites",
            json={
                "name": "Wikipedia",
                "base_url": "https://en.wikipedia.org/wiki/Backlink",
                "platform": "pool",
            },
        )
        assert wiki.status_code == 201, wiki.text
        created.append(wiki.json()["id"])
        approval = client.post(f"/api/v1/sites/{wiki.json()['id']}/pool-source/approval")
        assert approval.status_code == 200, approval.text

        collision = client.post(
            "/api/v1/sites",
            json={
                "name": "Client on the pool domain",
                "base_url": "https://wikipedia.org",
                "platform": "wordpress",
            },
        )
        assert collision.status_code == 409, collision.text
        assert "private blog network" in collision.text
    finally:
        for site_id in created:
            site = db.get(Site, site_id)
            if site is not None:
                db.delete(site)
                db.commit()
            _delete_audit_events(db, site_id)


def test_pool_approval_identity_comes_from_operator_key(client, db, monkeypatch):
    app.dependency_overrides.pop(require_api_key, None)
    monkeypatch.setattr(settings, "operator_api_keys", {"alice": SecretStr("alice-key")})
    response = client.post(
        "/api/v1/sites",
        headers={"X-API-Key": "alice-key"},
        json={
            "name": "Operator-approved pool",
            "base_url": "https://en.wikipedia.org/wiki/Information_retrieval",
            "platform": "pool",
        },
    )
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    try:
        missing = client.post(f"/api/v1/sites/{site_id}/pool-source/approval")
        assert missing.status_code == 401

        approved = client.post(
            f"/api/v1/sites/{site_id}/pool-source/approval",
            headers={"X-API-Key": "alice-key"},
            json={"approved_by": "mallory"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["pool_source_approved_by"] == "alice"
    finally:
        site = db.get(Site, site_id)
        if site is not None:
            db.delete(site)
            db.commit()
        _delete_audit_events(db, site_id)


def test_revoking_pool_source_expires_active_customer_suggestions(client, db):
    pool = _pool(db)
    customer = Site(
        name="Customer",
        base_url=f"https://customer-{uuid.uuid4().hex[:8]}.example.com",
        platform="html",
    )
    db.add(customer)
    db.flush()
    source = Article(
        site_id=customer.id,
        url=f"{customer.base_url}/source",
        title="Source",
        content_text="source",
    )
    target = Article(
        site_id=pool.id,
        url=f"https://example.com/{uuid.uuid4().hex}",
        title="Pool target",
        content_text="target",
    )
    db.add_all([source, target])
    db.flush()
    suggestions = [
        Suggestion(
            site_id=customer.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=0.8,
            status=status,
        )
        for status in ("pending", "approved", "rejected")
    ]
    db.add_all(suggestions)
    db.commit()
    try:
        response = client.delete(f"/api/v1/sites/{pool.id}/pool-source/approval")
        assert response.status_code == 200, response.text
        for suggestion in suggestions:
            db.refresh(suggestion)
        assert [suggestion.status for suggestion in suggestions] == [
            "expired",
            "expired",
            "rejected",
        ]
    finally:
        site_id = pool.id
        db.delete(pool)
        db.delete(customer)
        db.commit()
        _delete_audit_events(db, site_id)


def test_generic_service_key_cannot_supply_operator_identity(client, db, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "service-key")
    response = client.post(
        "/api/v1/sites",
        headers={"X-API-Key": "service-key"},
        json={
            "name": "Service-key pool",
            "base_url": "https://en.wikipedia.org/wiki/Search_engine",
            "platform": "pool",
        },
    )
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    try:
        approval = client.post(
            f"/api/v1/sites/{site_id}/pool-source/approval",
            headers={"X-API-Key": "service-key"},
        )
        assert approval.status_code == 401
        assert "operator-specific" in approval.text
    finally:
        site = db.get(Site, site_id)
        if site is not None:
            db.delete(site)
            db.commit()


def test_pool_source_is_quarantined_after_terminal_failures(monkeypatch, db):
    pool = _pool(db)
    customer = Site(
        name="Customer",
        base_url=f"https://customer-{uuid.uuid4().hex[:8]}.example.com",
        platform="html",
    )
    db.add(customer)
    db.flush()
    source = Article(
        site_id=customer.id,
        url=f"{customer.base_url}/source",
        title="Source",
        content_text="source",
    )
    target = Article(
        site_id=pool.id,
        url=f"https://example.com/{uuid.uuid4().hex}",
        title="Pool target",
        content_text="target",
    )
    db.add_all([source, target])
    db.flush()
    suggestion = Suggestion(
        site_id=customer.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        status="pending",
    )
    already_approved = Suggestion(
        site_id=customer.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=0.7,
        status="approved",
    )
    db.add_all([suggestion, already_approved])
    db.commit()
    monkeypatch.setattr(settings, "pool_quarantine_failure_threshold", 2)
    try:
        ingestion_task._record_pool_ingestion_failure(pool.id, RuntimeError("feed down"))
        ingestion_task._record_pool_ingestion_failure(pool.id, RuntimeError("still down"))
        db.refresh(pool)

        assert pool.pool_source_consecutive_failures == 2
        assert pool.pool_source_quarantined is True
        assert pool.pool_source_quarantine_reason == "still down"
        db.refresh(suggestion)
        assert suggestion.status == "expired"
        # Quarantine is automatic and reversible, so it withdraws the untouched
        # queue but not a decision an editor already made. Revocation, which is
        # a person deciding this source is not linkable, clears both.
        db.refresh(already_approved)
        assert already_approved.status == "approved"
        event = db.scalar(
            select(PoolSourceAuditEvent).where(PoolSourceAuditEvent.site_id == pool.id)
        )
        assert event is not None
        assert event.action == "quarantined"
        assert event.operator_id == "system"
        assert event.reason == "still down"
        with pytest.raises(PoolSourceQuarantinedError):
            require_approved_pool_source(pool)
    finally:
        site_id = pool.id
        db.delete(pool)
        db.delete(customer)
        db.commit()
        _delete_audit_events(db, site_id)


def test_pool_traceability_survives_site_deletion(client, db):
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "Deleted pool",
            "base_url": "https://en.wikipedia.org/wiki/Web_search_engine",
            "platform": "pool",
        },
    )
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    try:
        approved = client.post(f"/api/v1/sites/{site_id}/pool-source/approval")
        assert approved.status_code == 200, approved.text
        assert (
            client.delete(
                f"/api/v1/sites/{site_id}",
                params={"confirm_name": "Deleted pool"},
            ).status_code
            == 204
        )

        history = client.get(f"/api/v1/sites/{site_id}/pool-source/audit-events")
        assert history.status_code == 200
        assert history.json()[0]["action"] == "approved"
        assert history.json()[0]["site_name"] == "Deleted pool"
        assert history.json()[0]["site_base_url"].endswith("/Web_search_engine")
    finally:
        site = db.get(Site, site_id)
        if site is not None:
            db.delete(site)
            db.commit()
        _delete_audit_events(db, site_id)


def test_pool_routes_disallow_generation_and_publication(client, db):
    pool = _pool(db)
    try:
        response = client.get(f"/api/v1/sites/{pool.id}")
        assert response.json()["suggestion_slots_available"] == 0
        assert client.post(f"/api/v1/suggestions/{pool.id}").status_code == 409
        assert client.post(f"/api/v1/publish/{pool.id}").status_code == 409
    finally:
        db.delete(pool)
        db.commit()


def test_global_coordinator_discovers_new_daily_pool(monkeypatch, db):
    pool = _pool(db)
    queued: list[int] = []

    def fake_enqueue(_db, site_id, _kind, _fn, job_timeout):
        assert job_timeout == 3600
        queued.append(site_id)

    monkeypatch.setattr(pool_ingestion, "enqueue_job", fake_enqueue)
    try:
        assert pool_ingestion.enqueue_daily_pool_ingestions() == {
            "queued": 1,
            "skipped": 0,
            "failed": 0,
        }
        assert queued == [pool.id]
    finally:
        db.delete(pool)
        db.commit()


def test_global_coordinator_skips_unapproved_pool(monkeypatch, db):
    pool = _pool(db, approved=False)
    queued: list[int] = []
    monkeypatch.setattr(
        pool_ingestion,
        "enqueue_job",
        lambda _db, site_id, _kind, _fn, job_timeout: queued.append(site_id),
    )
    try:
        assert pool_ingestion.enqueue_daily_pool_ingestions() == {
            "queued": 0,
            "skipped": 0,
            "failed": 0,
        }
        assert queued == []
    finally:
        db.delete(pool)
        db.commit()


def test_coordinator_isolates_one_failing_source_and_keeps_going(monkeypatch, db):
    """A broken source costs its own crawl, not the whole daily run.

    The coordinator must also survive it: RQ schedules the next repeat only from
    the success path, so raising here would silently end the daily chain.
    """
    first, second = _pool(db), _pool(db)
    broken, healthy = min(first.id, second.id), max(first.id, second.id)
    alerts: list[tuple[str, dict, str, int | None]] = []

    def fake_enqueue(_db, site_id, _kind, _fn, job_timeout):
        if site_id == broken:
            raise RuntimeError("redis is down")

    monkeypatch.setattr(pool_ingestion, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(
        pool_ingestion,
        "send_alert",
        lambda subject, payload, *, kind, site_id: alerts.append((subject, payload, kind, site_id)),
    )
    try:
        result = pool_ingestion.enqueue_daily_pool_ingestions()

        assert result == {"queued": 1, "skipped": 0, "failed": 1}
        assert [alert[2] for alert in alerts] == ["pool_ingestion_enqueue_failed"]
        assert alerts[0][3] == broken
        assert "redis is down" in alerts[0][1]["error"]
        assert healthy  # the later source was still reached
    finally:
        db.delete(first)
        db.delete(second)
        db.commit()


def test_coordinator_is_registered_as_a_unique_repeating_retrying_job(monkeypatch):
    """Pin the enqueue contract the daily chain depends on.

    `repeat` is what makes it recur at all, `retry` is what keeps a worker death
    from ending the chain, and `unique` + the fixed id keep re-running the
    registration script from stacking duplicate coordinators.
    """
    captured: dict = {}

    def fake_enqueue(fn, **kwargs):
        captured["fn"] = fn
        captured.update(kwargs)
        return SimpleNamespace(id=kwargs["job_id"])

    monkeypatch.setattr(pool_ingestion.ingestion_queue, "enqueue", fake_enqueue)

    job = pool_ingestion.schedule_pool_ingestion()

    assert job.id == "linkmesh-pool-daily"
    assert captured["fn"] is pool_ingestion.enqueue_daily_pool_ingestions
    assert captured["unique"] is True
    assert captured["repeat"].times == settings.pool_poll_repeat_count
    assert captured["repeat"].intervals == [settings.pool_poll_interval_seconds]
    assert captured["retry"].max == 2


def test_coordinator_reports_a_total_failure_without_ending_the_repeat_chain(monkeypatch, db):
    pool = _pool(db)
    alerts: list[tuple[str, dict, str, int | None]] = []

    def exploding_session():
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(pool_ingestion, "SessionLocal", exploding_session)
    monkeypatch.setattr(
        pool_ingestion,
        "send_alert",
        lambda subject, payload, *, kind, site_id: alerts.append((subject, payload, kind, site_id)),
    )
    try:
        # Returning rather than raising is the whole point: RQ only schedules the
        # next repeat when the job succeeds.
        result = pool_ingestion.enqueue_daily_pool_ingestions()

        assert result["queued"] == 0
        assert "database is unreachable" in result["error"]
        assert [alert[2] for alert in alerts] == ["pool_coordinator_failed"]
        assert alerts[0][3] is None
    finally:
        db.delete(pool)
        db.commit()


def test_hybrid_can_target_pool_articles_but_keeps_customer_sources(db, site, monkeypatch):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(1.0) for _text in texts],
    )
    pool = _pool(db)
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/tomato-guide",
        title="Tomato canning guide",
        content_text=("A 2024 study found that safe tomato canning reduces infection risk by 30%."),
    )
    target = Article(
        site_id=pool.id,
        url="https://example.com/tomato-safety",
        title="Safe tomato canning",
        content_text="tomato canning jars boiling water safety",
    )
    db.add_all([source, target])
    db.add(ExternalLinkPolicy(site_id=site.id, external_links_enabled=True))
    db.flush()
    db.add_all(
        [
            Embedding(
                article_id=source.id,
                model=settings.embedding_model,
                vector=_vector(1.0),
                content_fingerprint=_fingerprint(source.title, source.content_text),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            ),
            Embedding(
                article_id=target.id,
                model=settings.embedding_model,
                vector=_vector(0.8, 0.6),
                content_fingerprint=_fingerprint(target.title, target.content_text),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            ),
        ]
    )
    db.commit()
    try:
        result = generate_suggestions(
            site.id,
            live_url_checker=_passing_live_url_checker(),
        )
        suggestion = db.scalar(select(Suggestion).where(Suggestion.site_id == site.id))
        assert suggestion is not None
        assert suggestion.source_article_id == source.id
        assert suggestion.target_article_id == target.id
        assert suggestion.score_components["live_url"]["eligible"] is True
        assert suggestion.score_components["citation_need"]["detector_version"] == (
            "citation_rules_en_v1"
        )
        assert (
            suggestion.feature_snapshot["citation_need"]
            == (suggestion.score_components["citation_need"])
        )
        assert result["citation_need_sources_detected"] == 1
        assert result["citation_need_sentences_detected"] == 1
    finally:
        db.delete(pool)
        db.commit()


def test_suggestion_api_identifies_internal_and_pool_targets(client, db, site):
    pool = _pool(db)
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="Source article",
        content_text="source",
    )
    internal_target = Article(
        site_id=site.id,
        url=f"{site.base_url}/internal-target",
        title="Internal target",
        content_text="internal target",
    )
    pool_target = Article(
        site_id=pool.id,
        url="https://example.com/pool-target",
        title="Pool target",
        content_text="pool target",
    )
    db.add_all([source, internal_target, pool_target])
    db.flush()
    internal = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=internal_target.id,
        method="baseline_cosine",
        score=0.9,
    )
    external = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=pool_target.id,
        method="hybrid_bm25",
        score=0.8,
    )
    db.add_all([internal, external])
    db.commit()
    try:
        response = client.get(f"/api/v1/suggestions/{site.id}")
        assert response.status_code == 200
        by_id = {item["id"]: item for item in response.json()}

        assert by_id[internal.id]["target_origin"] == "internal"
        assert by_id[internal.id]["target_site_name"] == site.name
        assert by_id[external.id]["target_origin"] == "content_pool"
        assert by_id[external.id]["target_site_name"] == pool.name

        page = client.get(
            "/api/v1/suggestions",
            params={"site_id": site.id, "include_total": True},
        )
        assert page.status_code == 200
        assert {item["target_origin"] for item in page.json()["items"]} == {
            "internal",
            "content_pool",
        }
    finally:
        db.delete(internal)
        db.delete(source)
        db.delete(internal_target)
        db.delete(pool)
        db.commit()


@pytest.mark.parametrize(
    ("approved", "quarantined"),
    [(False, False), (True, True)],
)
def test_hybrid_excludes_disabled_pool_sources(db, site, monkeypatch, approved, quarantined):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(1.0) for _text in texts],
    )
    pool = _pool(db, approved=approved)
    pool.pool_source_quarantined = quarantined
    db.add(ExternalLinkPolicy(site_id=site.id, external_links_enabled=True))
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/{uuid.uuid4().hex}",
        title="Tomato guide",
        content_text="tomato canning safety",
    )
    target = Article(
        site_id=pool.id,
        url=f"https://example.com/{uuid.uuid4().hex}",
        title="Tomato safety",
        content_text="tomato canning safety details",
    )
    db.add_all([source, target])
    db.flush()
    for article, vector in ((source, _vector(1.0)), (target, _vector(0.8, 0.6))):
        db.add(
            Embedding(
                article_id=article.id,
                model=settings.embedding_model,
                vector=vector,
                content_fingerprint=_fingerprint(article.title, article.content_text),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            )
        )
    db.commit()
    try:
        result = generate_suggestions(site.id)
        assert result["suggestions_created"] == 0
    finally:
        db.delete(pool)
        db.commit()


def test_pool_reconciliation_expires_customer_suggestions_to_missing_targets(db, site):
    pool = _pool(db)
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="Source",
        content_text="source",
    )
    target = Article(
        site_id=pool.id,
        url="https://example.com/removed",
        title="Removed",
        content_text="removed",
    )
    db.add_all([source, target])
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        status="pending",
    )
    run = IngestionRun(site_id=pool.id, status="running")
    db.add_all([suggestion, run])
    db.commit()
    try:
        _reconcile_snapshot(db, pool.id, run.id)
        db.commit()
        db.refresh(suggestion)
        assert suggestion.status == "expired"
        assert target.is_active is False
    finally:
        db.delete(pool)
        db.commit()
