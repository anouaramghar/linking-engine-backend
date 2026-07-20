"""Crawl-target validation and connect-time SSRF enforcement, with no real network.

DNS, sockets, and HTTP transports are stubbed at their respective seams.
"""

import socket
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from pydantic import ValidationError

from app.config import settings
from app.connectors import url_guard
from app.connectors.html_crawler import HTMLConnector
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    ValidatingNetworkBackend,
    request_guard,
    validate_url,
)
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


# -- connect-time address enforcement --------------------------------------


class _FakeSocket:
    def __init__(self, response: bytes = b"") -> None:
        self.response = response
        self.connected_to = None
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def setsockopt(self, *_option):
        pass

    def bind(self, _address):
        pass

    def connect(self, address):
        self.connected_to = address

    def sendall(self, _buffer):
        pass

    def recv(self, _max_bytes):
        response, self.response = self.response, b""
        return response

    def close(self):
        self.closed = True

    def getsockname(self):
        return ("192.0.2.10", 43210)

    def getpeername(self):
        return self.connected_to


def test_connect_time_dns_rebinding_to_private_address_is_blocked(monkeypatch):
    lookup_ports = []

    def rebinding_resolver(_host, port, **_kwargs):
        lookup_ports.append(port)
        address = "93.184.216.34" if port is None else "127.0.0.1"
        return [(2, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 0))]

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", rebinding_resolver)
    validate_url("http://rebind.example/")  # preflight sees a public address
    backend = ValidatingNetworkBackend(
        socket_factory=lambda *_args: pytest.fail("private address must not connect")
    )

    with httpx.Client(
        transport=SSRFProtectedTransport(network_backend=backend),
        trust_env=False,
        event_hooks={"request": [request_guard()]},
    ) as client:
        with pytest.raises(UnsafeURLError, match="127.0.0.1"):
            client.get("http://rebind.example/")

    assert lookup_ports == [None, 80]


def test_public_resolution_connects_to_the_validated_address():
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
    fake_socket = _FakeSocket(response)
    backend = ValidatingNetworkBackend(
        resolver=lambda _host, port, **_kwargs: [
            (2, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
        socket_factory=lambda *_args: fake_socket,
    )

    with httpx.Client(
        transport=SSRFProtectedTransport(network_backend=backend), trust_env=False
    ) as client:
        result = client.get("http://public.example/")

    assert result.text == "ok"
    assert fake_socket.connected_to == ("93.184.216.34", 80)


def test_connect_backend_rejects_if_any_resolved_address_is_private():
    backend = ValidatingNetworkBackend(
        resolver=lambda _host, port, **_kwargs: [
            (2, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (2, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", port)),
        ],
        socket_factory=lambda *_args: pytest.fail("mixed address set must not connect"),
    )

    with pytest.raises(UnsafeURLError, match="10.0.0.5"):
        backend.connect_tcp("mixed.example", 80)


def test_connect_backend_honors_allow_private():
    fake_socket = _FakeSocket()
    backend = ValidatingNetworkBackend(
        allow_private=True,
        resolver=lambda _host, port, **_kwargs: [
            (2, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
        socket_factory=lambda *_args: fake_socket,
    )

    backend.connect_tcp("local.example", 8080).close()

    assert fake_socket.connected_to == ("127.0.0.1", 8080)


class _CannedTLSStream(httpcore.NetworkStream):
    def __init__(self) -> None:
        self.response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        self.server_hostname = None

    def read(self, max_bytes, timeout=None):
        response, self.response = self.response, b""
        return response

    def write(self, buffer, timeout=None):
        pass

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.server_hostname = server_hostname
        return self

    def get_extra_info(self, info):
        return False if info == "is_readable" else None


class _CannedTLSBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self.stream = _CannedTLSStream()

    def connect_tcp(self, host, port, **_kwargs):
        return self.stream

    def connect_unix_socket(self, path, **_kwargs):
        raise AssertionError("unexpected UNIX socket")


def test_tls_preserves_original_hostname_for_sni_and_verification():
    backend = _CannedTLSBackend()

    with httpx.Client(
        transport=SSRFProtectedTransport(network_backend=backend), trust_env=False
    ) as client:
        assert client.get("https://original.example/").text == "ok"

    assert backend.stream.server_hostname == "original.example"


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


def test_connectors_use_connect_time_ssrf_transport(no_dns):
    wordpress = WordPressConnector(_site("https://example.com"))
    html = HTMLConnector(_site("https://example.com"))
    try:
        assert isinstance(wordpress.client._transport, SSRFProtectedTransport)
        assert isinstance(html.client._transport, SSRFProtectedTransport)
    finally:
        wordpress.client.close()
        html.client.close()


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
