"""Finding the feed of a content-pool source that was configured as a bare domain."""

import httpx
import pytest

from app.connectors.feed_discovery import (
    COMMON_FEED_PATHS,
    FeedNotFoundError,
    FeedPayloadError,
    feed_links_in_html,
    validate_feed_payload,
)
from app.connectors.rss_connector import RSSConnector
from app.models.site import Site


FEED = b"""<?xml version='1.0'?><rss version='2.0'><channel><title>News</title>
<item><guid>item-1</guid><title>Canning jars</title>
<link>https://en.wikipedia.org/wiki/Canning</link>
<description>Jars.</description></item></channel></rss>"""

HOME_PAGE_WITH_LINK = b"""<!doctype html><html><head>
<link rel="stylesheet" href="/style.css">
<link rel="alternate" type="application/rss+xml" title="Feed" href="/blog/feed.xml">
</head><body>Home</body></html>"""

HOME_PAGE_WITHOUT_LINK = (
    b"<!doctype html><html><head><title>Home</title></head><body>Hi</body></html>"
)


def _site(base_url: str = "https://en.wikipedia.org") -> Site:
    return Site(name="Pool", base_url=base_url, platform="pool")


def _connector(handler, base_url: str = "https://en.wikipedia.org") -> RSSConnector:
    return RSSConnector(_site(base_url), transport=httpx.MockTransport(handler))


def test_a_page_head_advertises_where_the_feed_lives():
    links = feed_links_in_html(HOME_PAGE_WITH_LINK, "https://en.wikipedia.org/")

    assert links == ["https://en.wikipedia.org/blog/feed.xml"]


def test_a_feed_advertised_on_another_host_is_not_followed():
    """A page can advertise any address, including one inside a private network.

    The address an operator approved is the only one this system may read, so a
    cross-host link is dropped here rather than left for the SSRF guard to refuse.
    """
    html = b"""<html><head>
    <link rel="alternate" type="application/rss+xml" href="https://elsewhere.example/feed">
    <link rel="alternate" type="application/atom+xml" href="//169.254.169.254/feed">
    <link rel="alternate" type="application/atom+xml" href="/local.xml">
    </head></html>"""

    assert feed_links_in_html(html, "https://en.wikipedia.org/") == [
        "https://en.wikipedia.org/local.xml"
    ]


def test_a_link_that_is_not_a_feed_is_ignored():
    html = b"""<html><head>
    <link rel="alternate" type="text/html" hreflang="fr" href="/fr/">
    <link rel="canonical" href="/canonical">
    <link rel="alternate" href="/no-type">
    </head></html>"""

    assert feed_links_in_html(html, "https://en.wikipedia.org/") == []


def test_the_configured_address_is_used_unchanged_when_it_serves_a_feed():
    """Every pool source today is a feed URL. Discovery must not cost them a request."""
    requested: list[str] = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, content=FEED)

    connector = _connector(handler, "https://en.wikipedia.org/feed.xml")
    try:
        articles = list(connector.fetch_articles())
    finally:
        connector.client.close()

    assert requested == ["https://en.wikipedia.org/feed.xml"]
    assert connector.feed_url is None
    assert [article.title for article in articles] == ["Canning jars"]


def test_a_bare_domain_resolves_to_the_feed_its_page_advertises():
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/blog/feed.xml":
            return httpx.Response(200, headers={"content-type": "text/xml"}, content=FEED)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=HOME_PAGE_WITH_LINK
        )

    connector = _connector(handler)
    try:
        articles = list(connector.fetch_articles())
        # The resolved address is kept, so a second call does not search again.
        metadata = connector.get_site_metadata()
    finally:
        connector.client.close()

    assert requested == ["/", "/blog/feed.xml", "/blog/feed.xml"]
    assert connector.feed_url == "https://en.wikipedia.org/blog/feed.xml"
    assert [article.title for article in articles] == ["Canning jars"]
    assert metadata.name == "News"


def test_a_page_without_a_link_falls_back_to_the_conventional_paths():
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/atom.xml":
            return httpx.Response(200, content=FEED)
        if request.url.path == "/rss.xml":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=HOME_PAGE_WITHOUT_LINK
        )

    connector = _connector(handler)
    try:
        articles = list(connector.fetch_articles())
    finally:
        connector.client.close()

    # /feed serves the home page again and is rejected as a page, not a feed.
    assert requested == ["/", "/feed", "/rss.xml", "/atom.xml"]
    assert connector.feed_url == "https://en.wikipedia.org/atom.xml"
    assert len(articles) == 1


def test_discovery_starts_below_a_configured_path():
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/blog/feed":
            return httpx.Response(200, content=FEED)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=HOME_PAGE_WITHOUT_LINK
        )

    connector = _connector(handler, "https://en.wikipedia.org/blog")
    try:
        list(connector.fetch_articles())
    finally:
        connector.client.close()

    assert requested == ["/blog", "/blog/feed"]
    assert connector.feed_url == "https://en.wikipedia.org/blog/feed"


def test_a_source_with_no_feed_anywhere_reports_what_was_tried():
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=HOME_PAGE_WITHOUT_LINK
        )

    connector = _connector(handler)
    try:
        with pytest.raises(FeedNotFoundError, match="0 advertised in <head>"):
            list(connector.fetch_articles())
    finally:
        connector.client.close()

    # One request for the address, then one per conventional path, and no more.
    assert len(requested) == 1 + len(COMMON_FEED_PATHS)


def test_a_broken_feed_is_reported_rather_than_searched_around():
    """A malformed or binary response at a feed URL is a broken feed.

    Probing five more paths on the same host would replace a precise diagnosis
    with a vague one and add five requests to a source that is already failing.
    """
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, content=b"\x00\x01not-a-feed")

    connector = _connector(handler, "https://en.wikipedia.org/feed.xml")
    try:
        with pytest.raises(FeedPayloadError, match="non-XML or binary response"):
            list(connector.fetch_articles())
    finally:
        connector.client.close()

    assert requested == ["/feed.xml"]


def test_the_content_type_rule_still_names_the_type_it_refused():
    with pytest.raises(FeedPayloadError, match="unsupported Content-Type 'text/plain'"):
        validate_feed_payload(FEED, "text/plain; charset=utf-8")
