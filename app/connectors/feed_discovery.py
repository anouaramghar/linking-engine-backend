"""Find the RSS/Atom feed of a source that was configured as a bare domain.

An operator adds a content-pool source by pasting the address they have, which is
normally the site itself and not its feed. `RSSConnector` needs the feed. This
module closes that gap in the order a browser does:

    1. the address itself, when it already serves a feed — the common case today,
       and it costs no extra request
    2. `<link rel="alternate" type="application/rss+xml">` in the page `<head>`,
       which is how a site publishes where its feed lives
    3. a short list of conventional paths, for sites that publish no such link

There is no fourth step here. A managed client site that has no feed is ingested
by the crawler instead, and that choice is made by `site.platform`, not here: a
content-pool source is read-only and feed-shaped by definition, so for this
caller "no feed" is a configuration error and must be reported as one.

Every request goes through the caller's client, so the SSRF guard, the pool
domain allowlist and the response-size limit apply to a discovered URL exactly as
they apply to a configured one.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings
from app.connectors.http_limits import ResponseTooLargeError, get_limited_response


_RSS_ATOM_MEDIA_TYPES = {
    "application/atom+xml",
    "application/rdf+xml",
    "application/rss+xml",
    "application/x-rss+xml",
    "application/xml",
    "text/xml",
}

#: Tried in order when a page advertises no feed. Kept short on purpose: each
#: entry is one request against a third-party host before the source fails.
COMMON_FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")


class FeedPayloadError(ValueError):
    """A response is not an RSS/Atom feed."""


class FeedNotFoundError(ValueError):
    """No feed was found at a source's address, in its markup, or at a known path."""


def _media_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _decoded_prefix(content: bytes) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content[:1024].decode("utf-16", errors="ignore").lstrip().lower()
    return content[:1024].decode("utf-8-sig", errors="ignore").lstrip().lower()


def looks_like_html(content: bytes) -> bool:
    """True for a web page, which is the signal that discovery is worth trying."""
    return _decoded_prefix(content).startswith(("<!doctype html", "<html"))


def validate_feed_payload(content: bytes, content_type: str | None) -> None:
    media_type = _media_type(content_type)
    if media_type is not None and media_type not in _RSS_ATOM_MEDIA_TYPES:
        raise FeedPayloadError(f"RSS/Atom source returned unsupported Content-Type {media_type!r}")
    prefix = _decoded_prefix(content)
    if not prefix.startswith("<"):
        raise FeedPayloadError("RSS/Atom source returned a non-XML or binary response")
    if prefix.startswith(("<!doctype html", "<html")):
        raise FeedPayloadError("RSS/Atom source returned an HTML page instead of a feed")


class _FeedLinkParser(HTMLParser):
    """Collect feed URLs from `<link rel="alternate">`, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        attributes = {name.lower(): (value or "") for name, value in attrs}
        rels = attributes.get("rel", "").lower().split()
        if "alternate" not in rels:
            return
        if _media_type(attributes.get("type")) not in _RSS_ATOM_MEDIA_TYPES:
            return
        if href := attributes.get("href", "").strip():
            self.hrefs.append(href)


def feed_links_in_html(html: bytes, base_url: str) -> list[str]:
    """Absolute, same-host feed URLs advertised by a page's `<head>`.

    A feed on another host is dropped rather than followed. The address an
    operator approved is the one this system is allowed to read, and a page can
    advertise anything — including an address inside a private network.
    """
    parser = _FeedLinkParser()
    try:
        parser.feed(html.decode("utf-8", errors="ignore"))
        parser.close()
    except (AssertionError, ValueError):
        # Broken markup yields whatever was parsed before the failure.
        pass
    base_host = (urlsplit(base_url).hostname or "").lower()
    found: list[str] = []
    for href in parser.hrefs:
        candidate = urljoin(base_url, href)
        split = urlsplit(candidate)
        if split.scheme not in ("http", "https"):
            continue
        if (split.hostname or "").lower() != base_host:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


@dataclass(frozen=True)
class DiscoveredFeed:
    """A feed URL and the response that proved it is one, so it is fetched once."""

    url: str
    content: bytes
    content_type: str | None


def _try_candidate(client: httpx.Client, url: str) -> DiscoveredFeed | None:
    """The feed at ``url``, or None. A failure of this candidate is not fatal."""
    try:
        response = get_limited_response(client, url, max_bytes=settings.pool_max_response_bytes)
        validate_feed_payload(response.content, response.content_type)
    except (httpx.HTTPError, FeedPayloadError, ResponseTooLargeError):
        # A policy failure — an unapproved domain, a blocked address — is not
        # caught here. It is a decision about this source, not a dead candidate.
        return None
    return DiscoveredFeed(url=url, content=response.content, content_type=response.content_type)


def _candidate_urls(base_url: str, html: bytes) -> tuple[list[str], int]:
    """Feed URLs to try in order, and how many of them the page advertised."""
    candidates = feed_links_in_html(html, base_url)
    advertised = len(candidates)
    root = base_url if base_url.endswith("/") else base_url + "/"
    for path in COMMON_FEED_PATHS:
        candidate = urljoin(root, path.lstrip("/"))
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates, advertised


def discover_feed(client: httpx.Client, base_url: str, *, html: bytes) -> DiscoveredFeed:
    """Find the feed of a source whose address served ``html`` instead of a feed.

    The caller has already fetched ``base_url`` and found a web page there, so
    that response is passed in rather than requested again.
    """
    candidates, advertised = _candidate_urls(base_url, html)
    for candidate in candidates:
        if found := _try_candidate(client, candidate):
            return found
    raise FeedNotFoundError(
        f"{base_url} serves a web page and no feed was found: "
        f"{advertised} advertised in <head>, "
        f"{len(candidates) - advertised} conventional paths tried"
    )
