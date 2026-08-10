"""Generic HTML connector — sitemap at {base_url}/sitemap_index.xml (uniform across the fleet,
ultimate-sitemap-parser dropped per supervisor decision), content extraction via trafilatura.

ponytail: sequential httpx fetching — fine for pilot-scale sites; switch to the Scrapy
crawler implementation when fleet-scale crawling (v5) needs concurrency/politeness.
"""

import logging
from collections.abc import Iterator
from time import monotonic
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from lxml import etree, html as lxml_html

from app.config import settings
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    LinkOutcome,
    OutboundLink,
    SiteMetadata,
)
from app.connectors.http_limits import check_crawl_deadline, get_limited_http_response
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)
from app.models.suggestion import Suggestion

#: Anchors are phrases; a link wrapping a whole section is stored truncated.
_MAX_ANCHOR_TEXT_CHARS = 300
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
# Sitemap XML is untrusted input — no entity resolution, no network fetches
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

logger = logging.getLogger(__name__)


class HTMLConnector(ContentConnector):
    def __init__(self, site):
        super().__init__(site)
        allow_private = settings.allow_unsafe_crawl_targets
        validate_url(site.base_url, allow_private=allow_private, resolve_dns=False)
        self.client = httpx.Client(
            transport=SSRFProtectedTransport(allow_private=allow_private),
            trust_env=False,
            timeout=30,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "LinkMesh/0.1"},
            event_hooks={"request": [request_guard(allow_private=allow_private)]},
        )
        self._host = urlparse(site.base_url).netloc
        self._crawl_started_at: float | None = None

    def _check_crawl_budget(self) -> None:
        if self._crawl_started_at is None:
            self._crawl_started_at = monotonic()
        check_crawl_deadline(self._crawl_started_at)

    def _sitemap_urls(self) -> list[str]:
        """sitemap_index.xml -> child sitemaps -> page URLs. Plain sitemaps also handled."""
        index_url = self.site.base_url.rstrip("/") + "/sitemap_index.xml"
        urls: list[str] = []
        sitemap_urls = self._parse_sitemap(
            index_url,
            "//sm:sitemap/sm:loc",
            max_items=settings.crawl_max_sitemaps,
        ) or [index_url]
        for sitemap_url in sitemap_urls:
            urls += self._parse_sitemap(
                sitemap_url,
                "//sm:url/sm:loc",
                max_items=settings.crawl_max_sitemap_urls,
            )
            if len(urls) > settings.crawl_max_sitemap_urls:
                raise ValueError(f"sitemap URL count exceeded {settings.crawl_max_sitemap_urls}")
        return urls

    def _parse_sitemap(self, url: str, xpath: str, *, max_items: int | None = None) -> list[str]:
        self._check_crawl_budget()
        resp = get_limited_http_response(
            self.client,
            url,
            max_bytes=settings.crawl_max_response_bytes,
            crawl_started_at=self._crawl_started_at,
        )
        resp.raise_for_status()
        tree = etree.fromstring(resp.content, parser=_XML_PARSER)
        locs = [loc.text.strip() for loc in tree.xpath(xpath, namespaces=SITEMAP_NS) if loc.text]
        if max_items is not None and len(locs) > max_items:
            raise ValueError(f"sitemap item count exceeded {max_items}")
        return [loc for loc in locs if self._same_origin(loc)]

    def _same_origin(self, url: str) -> bool:
        """Sitemap traversal stays on the site's own host (Phase 0, finding #1)."""
        try:
            # Host check only here; the pinned transport checks addresses at fetch time.
            validate_url(url, allow_private=True, same_host_as=self.site.base_url)
        except UnsafeURLError as e:
            logger.warning("skipping sitemap URL: %s", e)
            return False
        return True

    def fetch_articles(self) -> Iterator[ArticleData]:
        for article_number, url in enumerate(self._sitemap_urls(), start=1):
            if article_number > settings.crawl_max_articles:
                raise ValueError(f"crawl article count exceeded {settings.crawl_max_articles}")
            article = self.fetch_article_by_url(url)
            if article:
                yield article

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        self._check_crawl_budget()
        resp = get_limited_http_response(
            self.client,
            url,
            max_bytes=settings.crawl_max_response_bytes,
            crawl_started_at=self._crawl_started_at,
        )
        if resp.status_code != 200:
            return None
        doc = trafilatura.bare_extraction(resp.text, url=url, with_metadata=True)
        if doc is None or not doc.text:
            return None  # not an article page (home, category listing...)
        if len(doc.text) > settings.crawl_max_article_chars:
            raise ValueError(
                f"article content exceeded {settings.crawl_max_article_chars} characters"
            )
        tree = lxml_html.fromstring(resp.text)
        internal = [
            OutboundLink(
                url=absolute,
                anchor_text=" ".join(anchor.text_content().split())[:_MAX_ANCHOR_TEXT_CHARS]
                or None,
            )
            for anchor in tree.xpath("//a[@href]")
            if urlparse(absolute := urljoin(url, anchor.get("href"))).netloc == self._host
        ]
        if len(internal) > settings.crawl_max_links_per_article:
            raise ValueError(f"article link count exceeded {settings.crawl_max_links_per_article}")
        return ArticleData(
            url=url,
            title=doc.title or url,
            content_text=doc.text,
            content_html=None,
            language=None,
            published_at=None,  # trafilatura dates are unreliable; left for v2 heuristics
            taxonomies=[],  # no structured source on static HTML (A14)
            outbound_internal_links=internal,
        )

    def get_site_metadata(self) -> SiteMetadata:
        return SiteMetadata(
            name=self.site.name,
            base_url=self.site.base_url,
            platform="html",
            article_count=len(self._sitemap_urls()),
        )

    def supports_incremental_sync(self) -> bool:
        return False

    def apply_links(
        self, suggestions: list[Suggestion], *, dry_run: bool = False
    ) -> list[LinkOutcome]:
        # A3 resolved: design ready (FTP hypothesis documented), no implementation —
        # HTML sites are secondary, WordPress is the v1 priority.
        raise NotImplementedError("writing to static HTML sites is not supported in v1 (A3)")
