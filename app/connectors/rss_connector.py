"""Read-only RSS/Atom content-pool connector."""

from calendar import timegm
from collections.abc import Iterator
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser

import feedparser
import httpx

from app.config import settings
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    LinkOutcome,
    SiteMetadata,
    TaxonomyData,
)
from app.connectors.http_limits import get_limited_response
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)
from app.models.site import Site
from app.models.suggestion import Suggestion
from app.services.pool_source_policy import pool_request_guard

_RSS_ATOM_MEDIA_TYPES = {
    "application/atom+xml",
    "application/rdf+xml",
    "application/rss+xml",
    "application/x-rss+xml",
    "application/xml",
    "text/xml",
}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if value := data.strip():
            self.parts.append(value)


def _text(fragment: object) -> str:
    value = str(fragment or "").strip()
    if not value:
        return ""
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
    except ValueError:
        return value
    return " ".join(parser.parts)


def _published_at(entry: dict) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)


def _entry_html(entry: dict) -> str:
    content = entry.get("content") or []
    if content and isinstance(content[0], dict):
        return str(content[0].get("value") or "")
    return str(entry.get("summary") or entry.get("description") or "")


def _external_id(value: str) -> str:
    return value if len(value) <= 255 else f"rss:{sha256(value.encode()).hexdigest()}"


def _media_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _decoded_prefix(content: bytes) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content[:1024].decode("utf-16", errors="ignore").lstrip().lower()
    return content[:1024].decode("utf-8-sig", errors="ignore").lstrip().lower()


def _validate_feed_payload(content: bytes, content_type: str | None) -> None:
    media_type = _media_type(content_type)
    if media_type is not None and media_type not in _RSS_ATOM_MEDIA_TYPES:
        raise ValueError(f"RSS/Atom source returned unsupported Content-Type {media_type!r}")
    prefix = _decoded_prefix(content)
    if not prefix.startswith("<"):
        raise ValueError("RSS/Atom source returned a non-XML or binary response")
    if prefix.startswith(("<!doctype html", "<html")):
        raise ValueError("RSS/Atom source returned an HTML page instead of a feed")


class RSSConnector(ContentConnector):
    def __init__(
        self,
        site: Site,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(site)
        allow_private = settings.allow_unsafe_crawl_targets
        validate_url(
            site.base_url,
            allow_private=allow_private,
            require_https=not allow_private,
            resolve_dns=False,
        )
        self.client = httpx.Client(
            transport=transport or SSRFProtectedTransport(allow_private=allow_private),
            trust_env=False,
            timeout=settings.pool_source_timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": settings.pool_http_user_agent},
            event_hooks={
                "request": [
                    request_guard(
                        allow_private=allow_private,
                        require_https=not allow_private,
                    ),
                    pool_request_guard,
                ]
            },
        )

    def _feed(self):
        response = get_limited_response(
            self.client,
            self.site.base_url,
            max_bytes=settings.pool_max_response_bytes,
        )
        _validate_feed_payload(response.content, response.content_type)
        parsed = feedparser.parse(response.content)
        if parsed.bozo:
            raise ValueError(f"invalid RSS/Atom feed: {parsed.bozo_exception}")
        if not str(parsed.version or "").lower().startswith(("rss", "atom")):
            raise ValueError("invalid RSS/Atom feed: unsupported or missing feed version")
        return parsed

    def _to_article(self, entry: dict, feed_language: str | None) -> ArticleData | None:
        url = str(entry.get("link") or "").strip()
        if not url:
            return None
        try:
            validate_url(
                url,
                allow_private=settings.allow_unsafe_crawl_targets,
                require_https=not settings.allow_unsafe_crawl_targets,
                resolve_dns=False,
            )
        except UnsafeURLError:
            return None
        if len(url) > 2048:
            return None
        content_html = _entry_html(entry)[: settings.pool_max_article_chars]
        title = (_text(entry.get("title")) or url)[: settings.pool_max_title_chars]
        content_text = (_text(content_html) or _text(entry.get("summary")) or title)[
            : settings.pool_max_article_chars
        ]
        tags: list[TaxonomyData] = []
        seen_tags: set[str] = set()
        for tag in entry.get("tags") or []:
            name = _text(tag.get("term") if isinstance(tag, dict) else "")
            if name and name.casefold() not in seen_tags:
                seen_tags.add(name.casefold())
                tags.append(TaxonomyData(kind="tag", name=name[:255]))
        language = str(entry.get("language") or feed_language or "").strip() or None
        return ArticleData(
            url=url,
            title=title,
            content_text=content_text,
            content_html=content_html or None,
            external_id=_external_id(str(entry.get("id") or url)),
            language=language[:10] if language else None,
            published_at=_published_at(entry),
            taxonomies=tags,
            outbound_internal_links=[],
        )

    def fetch_articles(self) -> Iterator[ArticleData]:
        parsed = self._feed()
        feed_language = parsed.feed.get("language")
        for entry in parsed.entries[: settings.pool_max_articles_per_source]:
            if article := self._to_article(entry, feed_language):
                yield article

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        return next((article for article in self.fetch_articles() if article.url == url), None)

    def get_site_metadata(self) -> SiteMetadata:
        parsed = self._feed()
        return SiteMetadata(
            name=_text(parsed.feed.get("title")) or self.site.name,
            base_url=self.site.base_url,
            platform="pool",
            article_count=min(len(parsed.entries), settings.pool_max_articles_per_source),
        )

    def supports_incremental_sync(self) -> bool:
        return True

    def apply_links(
        self, suggestions: list[Suggestion], *, dry_run: bool = False
    ) -> list[LinkOutcome]:
        raise NotImplementedError("content-pool sources are read-only")
