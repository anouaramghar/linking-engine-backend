"""Crawl-target validation (Phase 0, finding #1 — SSRF), no network.

DNS is stubbed via url_guard._addresses / socket.getaddrinfo; HTTP via MockTransport.
"""

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.config import settings
from app.connectors import url_guard
from app.connectors.html_crawler import HTMLConnector
from app.connectors.url_guard import UnsafeURLError, request_guard, validate_url
from app.connectors.wordpress import WordPressConnector
from app.schemas.site import SiteCreate


@pytest.fixture(autouse=True)
def strict_crawl_targets(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", False)


@pytest.fixture
def no_dns(monkeypatch):
    """Any hostname lookup in these tests is a bug — fail loudly."""

    def _boom(host):
        raise AssertionError(f"unexpected DNS lookup for {host!r}")

    monkeypatch.setattr(url_guard, "_addresses", lambda host: _boom(host))


# -- validate_url ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/sitemap.xml",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "https://user@example.com/",
        "https://",
        "/relative/path",
    ],
)
def test_rejects_malformed_and_credentialed_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/wp-json",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",  # CGNAT
    ],
)
def test_rejects_non_public_ip_literals(url):
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(url)


def test_rejects_hostname_resolving_to_private_address(monkeypatch):
    monkeypatch.setattr(
        url_guard.socket,
        "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("10.13.37.1", 0))],
    )
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url("https://internal.corp/")


def test_accepts_hostname_resolving_to_public_address(monkeypatch):
    monkeypatch.setattr(
        url_guard.socket,
        "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    validate_url("https://example.com/")


def test_rejects_unresolvable_hostname(monkeypatch):
    def _fail(*a, **kw):
        raise url_guard.socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", _fail)
    with pytest.raises(UnsafeURLError, match="cannot resolve"):
        validate_url("https://no-such-host.example/")


def test_allow_private_permits_local_targets(no_dns):
    validate_url("http://127.0.0.1:8080/wp-json", allow_private=True)


def test_require_https_rejects_plain_http(no_dns):
    with pytest.raises(UnsafeURLError, match="HTTPS"):
        validate_url("http://example.com/", require_https=True, resolve_dns=False)


def test_same_host_enforced(no_dns):
    validate_url("https://example.com/page", same_host_as="https://example.com", allow_private=True)
    with pytest.raises(UnsafeURLError, match="not on host"):
        validate_url(
            "https://evil.com/page", same_host_as="https://example.com", allow_private=True
        )


def test_resolve_dns_false_still_checks_ip_literals():
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url("http://169.254.169.254/", resolve_dns=False)


# -- request guard: redirects ----------------------------------------------


def test_redirect_to_private_address_is_blocked():
    reached = []

    def handler(request):
        reached.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        event_hooks={"request": [request_guard()]},
    )
    with pytest.raises(UnsafeURLError, match="non-public"):
        client.get("https://93.184.216.34/start")
    assert reached == ["https://93.184.216.34/start"]  # the metadata hop never fired


# -- connectors -------------------------------------------------------------


def _site(base_url, wp_username=None, wp_app_password=None):
    return SimpleNamespace(
        name="s", base_url=base_url, wp_username=wp_username, wp_app_password=wp_app_password
    )


def test_wordpress_connector_requires_https_with_credentials(no_dns):
    with pytest.raises(UnsafeURLError, match="HTTPS"):
        WordPressConnector(_site("http://example.com", wp_username="admin", wp_app_password="pw"))
    # read-only http (no credentials) stays allowed
    WordPressConnector(_site("http://example.com"))


def test_html_connector_skips_cross_origin_sitemap_entries(no_dns):
    index = b"""<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/post-sitemap.xml</loc></sitemap>
      <sitemap><loc>https://evil.com/sitemap.xml</loc></sitemap>
    </sitemapindex>"""
    child = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a-post/</loc></url>
      <url><loc>http://169.254.169.254/latest/</loc></url>
      <url><loc>https://other.org/page/</loc></url>
    </urlset>"""
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        body = index if "sitemap_index" in request.url.path else child
        return httpx.Response(200, content=body)

    connector = HTMLConnector(_site("https://example.com"))
    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert connector._sitemap_urls() == ["https://example.com/a-post/"]
    assert "https://evil.com/sitemap.xml" not in fetched


def test_sitemap_xml_entities_are_not_resolved(no_dns):
    evil = b"""<?xml version="1.0"?>
    <!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>&xxe;</loc></url>
    </urlset>"""

    def handler(request):
        return httpx.Response(200, content=evil)

    connector = HTMLConnector(_site("https://example.com"))
    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert connector._parse_sitemap("https://example.com/s.xml", "//sm:url/sm:loc") == []


# -- site creation schema ----------------------------------------------------


def _payload(**overrides):
    return {
        "name": "s",
        "base_url": "https://example.com",
        "platform": "wordpress",
    } | overrides


def test_site_create_rejects_credentialed_url():
    with pytest.raises(ValidationError, match="credentials in URL"):
        SiteCreate(**_payload(base_url="https://user:pw@example.com"))


def test_site_create_rejects_http_with_wp_credentials():
    with pytest.raises(ValidationError, match="HTTPS"):
        SiteCreate(**_payload(base_url="http://example.com", wp_username="a", wp_app_password="b"))


def test_site_create_rejects_private_ip_literal():
    with pytest.raises(ValidationError, match="non-public"):
        SiteCreate(**_payload(base_url="http://192.168.1.10"))


def test_site_create_allows_private_target_with_dev_flag(monkeypatch):
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", True)
    site = SiteCreate(**_payload(base_url="http://localhost:8080/"))
    assert site.base_url == "http://localhost:8080"
