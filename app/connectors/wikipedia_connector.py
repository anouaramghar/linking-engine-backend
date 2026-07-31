"""Read-only Wikipedia content-pool connector using the MediaWiki API."""

from collections.abc import Iterator
from datetime import datetime
from urllib.parse import unquote, urlparse

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
        validate_url(site.base_url, allow_private=allow_private, resolve_dns=False)
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
            headers={"User-Agent": "LinkMesh/0.1 (+content-pool)"},
            event_hooks={"request": [request_guard(allow_private=allow_private)]},
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

    def _pages(self, **query) -> list[dict]:
        response = self.client.get(
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
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError(f"Wikipedia API error: {payload['error'].get('info', 'unknown')}")
        return payload.get("query", {}).get("pages", [])

    def _to_article(self, page: dict) -> ArticleData | None:
        content = str(page.get("extract") or "").strip()
        url = str(page.get("fullurl") or "").strip()
        if page.get("missing") or not url or not content:
            return None
        try:
            validate_url(
                url,
                allow_private=settings.allow_unsafe_crawl_targets,
                same_host_as=self.site.base_url,
                resolve_dns=False,
            )
        except UnsafeURLError:
            return None
        revisions = page.get("revisions") or []
        return ArticleData(
            url=url,
            title=str(page.get("title") or url),
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
        for page in self._pages(
            generator="search",
            gsrsearch=self.seed_title,
            gsrlimit=settings.pool_max_articles_per_source,
            gsrnamespace=0,
        ):
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
