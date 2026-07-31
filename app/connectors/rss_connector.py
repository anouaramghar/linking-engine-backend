"""Read-only RSS/Atom content-pool connector."""

from calendar import timegm
from collections.abc import Iterator
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser

import feedparser
import httpx

from app.config import settings
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)
from app.models.site import Site
from app.models.suggestion import Suggestion


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


class RSSConnector(ContentConnector):
    def __init__(
        self,
        site: Site,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(site)
        allow_private = settings.allow_unsafe_crawl_targets
        validate_url(site.base_url, allow_private=allow_private, resolve_dns=False)
        self.client = httpx.Client(
            transport=transport or SSRFProtectedTransport(allow_private=allow_private),
            trust_env=False,
            timeout=settings.pool_source_timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "LinkMesh/0.1 (+content-pool)"},
            event_hooks={"request": [request_guard(allow_private=allow_private)]},
        )

    def _feed(self):
        response = self.client.get(self.site.base_url)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"invalid RSS/Atom feed: {parsed.bozo_exception}")
        return parsed

    def _to_article(self, entry: dict, feed_language: str | None) -> ArticleData | None:
        url = str(entry.get("link") or "").strip()
        if not url:
            return None
        try:
            validate_url(
                url,
                allow_private=settings.allow_unsafe_crawl_targets,
                resolve_dns=False,
            )
        except UnsafeURLError:
            return None
        content_html = _entry_html(entry)
        title = _text(entry.get("title")) or url
        content_text = _text(content_html) or _text(entry.get("summary")) or title
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
            outbound_internal_urls=[],
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

    def apply_link(self, suggestion: Suggestion) -> None:
        raise NotImplementedError("content-pool sources are read-only")
