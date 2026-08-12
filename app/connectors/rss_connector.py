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
    PlannedEditOutcome,
    SiteMetadata,
    TaxonomyData,
)
from app.connectors.feed_discovery import (
    FeedPayloadError,
    discover_feed,
    looks_like_html,
    validate_feed_payload,
)
from app.connectors.http_limits import get_limited_response
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)
from app.models.site import Site
from app.services.pool_source_policy import pool_request_guard


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
        #: Set once the site's address turns out to be a page rather than a feed.
        self.feed_url: str | None = None
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

    def _feed_payload(self) -> bytes:
        """The feed bytes, resolving the feed URL once if the source is a web page.

        An operator normally has the site's address, not its feed's. When the
        address serves a page, `discover_feed` finds the feed the page advertises
        or one at a conventional path, and the answer is kept for the life of this
        connector so the search runs once per ingestion rather than once per call.

        A source whose address already serves a feed — every pool source today —
        takes the first branch and makes exactly the one request it always made.
        """
        url = self.feed_url or self.site.base_url
        response = get_limited_response(
            self.client,
            url,
            max_bytes=settings.pool_max_response_bytes,
        )
        try:
            validate_feed_payload(response.content, response.content_type)
        except FeedPayloadError:
            # Only a web page starts a search. A malformed, binary or oversized
            # response at a known feed URL is a broken feed, and reporting that
            # is more useful than probing five more paths on the same host.
            if self.feed_url is not None or not looks_like_html(response.content):
                raise
            found = discover_feed(self.client, self.site.base_url, html=response.content)
            self.feed_url = found.url
            return found.content
        return response.content

    def _feed(self):
        parsed = feedparser.parse(self._feed_payload())
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

    def apply_planned_edit(
        self, source, *, original_html: str, updated_html: str
    ) -> PlannedEditOutcome:
        raise NotImplementedError("content-pool sources are read-only")
