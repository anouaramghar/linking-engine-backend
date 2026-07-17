"""Generic HTML connector — sitemap at {base_url}/sitemap_index.xml (uniform across the fleet,
ultimate-sitemap-parser dropped per supervisor decision), content extraction via trafilatura.

ponytail: sequential httpx fetching — fine for pilot-scale sites; switch to the Scrapy
crawler implementation when fleet-scale crawling (v5) needs concurrency/politeness.
"""

import logging
from collections.abc import Iterator
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from lxml import etree, html as lxml_html

from app.config import settings
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)
from app.models.suggestion import Suggestion

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

    def _sitemap_urls(self) -> list[str]:
        """sitemap_index.xml -> child sitemaps -> page URLs. Plain sitemaps also handled."""
        index_url = self.site.base_url.rstrip("/") + "/sitemap_index.xml"
        urls: list[str] = []
        for sitemap_url in self._parse_sitemap(index_url, "//sm:sitemap/sm:loc") or [index_url]:
            urls += self._parse_sitemap(sitemap_url, "//sm:url/sm:loc")
        return urls

    def _parse_sitemap(self, url: str, xpath: str) -> list[str]:
        resp = self.client.get(url)
        resp.raise_for_status()
        tree = etree.fromstring(resp.content, parser=_XML_PARSER)
        locs = [loc.text.strip() for loc in tree.xpath(xpath, namespaces=SITEMAP_NS) if loc.text]
        return [loc for loc in locs if self._same_origin(loc)]

    def _same_origin(self, url: str) -> bool:
        """Sitemap traversal stays on the site's own host (Phase 0, finding #1)."""
        try:
            # host check only here — the request guard does the address check at fetch time
            validate_url(url, allow_private=True, same_host_as=self.site.base_url)
        except UnsafeURLError as e:
            logger.warning("skipping sitemap URL: %s", e)
            return False
        return True

    def fetch_articles(self) -> Iterator[ArticleData]:
        for url in self._sitemap_urls():
            article = self.fetch_article_by_url(url)
            if article:
                yield article

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        resp = self.client.get(url)
        if resp.status_code != 200:
            return None
        doc = trafilatura.bare_extraction(resp.text, url=url, with_metadata=True)
        if doc is None or not doc.text:
            return None  # not an article page (home, category listing...)
        tree = lxml_html.fromstring(resp.text)
        internal = [
            urljoin(url, href)
            for href in tree.xpath("//a/@href")
            if urlparse(urljoin(url, href)).netloc == self._host
        ]
        return ArticleData(
            url=url,
            title=doc.title or url,
            content_text=doc.text,
            content_html=None,
            language=None,
            published_at=None,  # trafilatura dates are unreliable; left for v2 heuristics
            taxonomies=[],  # no structured source on static HTML (A14)
            outbound_internal_urls=internal,
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

    def apply_link(self, suggestion: Suggestion) -> None:
        # A3 resolved: design ready (FTP hypothesis documented), no implementation —
        # HTML sites are secondary, WordPress is the v1 priority.
        raise NotImplementedError("writing to static HTML sites is not supported in v1 (A3)")
