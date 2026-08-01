"""Read-only Wikipedia content-pool connector using the MediaWiki API."""

import json
import time
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import unquote, urlparse

import httpx

from app.config import settings
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
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


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class WikipediaConnector(ContentConnector):
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
        if not self.supports_url(site.base_url):
            raise ValueError("Wikipedia pool sources must use a wikipedia.org article URL")
        parsed = urlparse(site.base_url)
        self.host = parsed.hostname or ""
        self.language = self.host.split(".", 1)[0] if "." in self.host else None
        self.seed_title = unquote(parsed.path.removeprefix("/wiki/")).replace("_", " ")
        self.client = httpx.Client(
            base_url=f"{parsed.scheme}://{self.host}",
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

    @staticmethod
    def supports_url(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and (host == "wikipedia.org" or host.endswith(".wikipedia.org"))
            and parsed.path.startswith("/wiki/")
            and bool(parsed.path.removeprefix("/wiki/"))
        )

    def _pages(self, *, result_limit: int | None = None, **query) -> list[dict]:
        pages: list[dict] = []
        continuation: dict = {}
        max_requests = 1
        if result_limit:
            page_size = max(1, int(query.get("gsrlimit") or result_limit))
            max_requests = (result_limit + page_size - 1) // page_size + 1
        for request_number in range(max_requests):
            response = get_limited_response(
                self.client,
                "/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": 2,
                    "redirects": 1,
                    "prop": "extracts|info|revisions",
                    "inprop": "url",
                    "explaintext": 1,
                    "rvprop": "timestamp",
                    "rvlimit": 1,
                    **query,
                    **continuation,
                },
                max_bytes=settings.pool_max_response_bytes,
            )
            media_type = (response.content_type or "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" and not media_type.endswith("+json"):
                raise ValueError(
                    "Wikipedia API returned unsupported Content-Type "
                    f"{media_type or 'missing'}"
                )
            try:
                payload = json.loads(response.content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Wikipedia API returned invalid JSON") from error
            if not isinstance(payload, dict):
                raise TypeError("Wikipedia API returned an invalid payload")
            api_error = payload.get("error")
            if api_error is not None:
                info = (
                    api_error.get("info", "unknown")
                    if isinstance(api_error, dict)
                    else api_error
                )
                raise ValueError(f"Wikipedia API error: {info}")
            pages.extend(payload.get("query", {}).get("pages", []))
            if result_limit is not None and len(pages) >= result_limit:
                return pages[:result_limit]
            continuation = payload.get("continue") or {}
            if not continuation:
                return pages
            if request_number + 1 >= max_requests:
                raise ValueError("Wikipedia API pagination exceeded request limit")
            time.sleep(settings.pool_request_delay_seconds)

    def _to_article(self, page: dict) -> ArticleData | None:
        content = str(page.get("extract") or "").strip()
        url = str(page.get("fullurl") or "").strip()
        if page.get("missing") or not url or not content or len(url) > 2048:
            return None
        content = content[: settings.pool_max_article_chars]
        try:
            validate_url(
                url,
                allow_private=settings.allow_unsafe_crawl_targets,
                require_https=not settings.allow_unsafe_crawl_targets,
                same_host_as=self.site.base_url,
                resolve_dns=False,
            )
        except UnsafeURLError:
            return None
        revisions = page.get("revisions") or []
        return ArticleData(
            url=url,
            title=str(page.get("title") or url)[: settings.pool_max_title_chars],
            content_text=content,
            external_id=str(page.get("pageid") or url),
            language=self.language,
            published_at=_timestamp(revisions[0].get("timestamp")) if revisions else None,
            taxonomies=[
                TaxonomyData(kind="tag", name="Wikipedia"),
                TaxonomyData(kind="tag", name=self.seed_title[:255]),
            ],
            outbound_internal_urls=[],
        )

    def fetch_articles(self) -> Iterator[ArticleData]:
        pages = self._pages(
            result_limit=settings.pool_max_articles_per_source,
            generator="search",
            gsrsearch=self.seed_title,
            # MediaWiki caps regular-user search results at 20 per request.
            gsrlimit=min(settings.pool_max_articles_per_source, 20),
            gsrnamespace=0,
        )
        for page in pages:
            if article := self._to_article(page):
                yield article

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        if not self.supports_url(url) or urlparse(url).hostname != self.host:
            return None
        title = unquote(urlparse(url).path.removeprefix("/wiki/")).replace("_", " ")
        return next(
            (article for page in self._pages(titles=title) if (article := self._to_article(page))),
            None,
        )

    def get_site_metadata(self) -> SiteMetadata:
        articles = list(self.fetch_articles())
        return SiteMetadata(
            name=self.site.name,
            base_url=self.site.base_url,
            platform="pool",
            article_count=len(articles),
        )

    def supports_incremental_sync(self) -> bool:
        return True

    def apply_link(self, suggestion: Suggestion) -> None:
        raise NotImplementedError("content-pool sources are read-only")
