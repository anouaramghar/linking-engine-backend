"""Generic HTML connector — sitemap at {base_url}/sitemap_index.xml (uniform across the fleet,
ultimate-sitemap-parser dropped per supervisor decision), content extraction via trafilatura.

ponytail: sequential httpx fetching — fine for pilot-scale sites; switch to the Scrapy
crawler implementation when fleet-scale crawling (v5) needs concurrency/politeness.
"""

import logging
from collections import deque
from collections.abc import Iterator
from html import escape
from time import monotonic
from urllib.robotparser import RobotFileParser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
import trafilatura
from lxml import etree, html as lxml_html

from app.config import settings
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    DiscoveryObservation,
    LinkOutcome,
    LinkPreview,
    OutboundLink,
    PlannedEditOutcome,
    SiteMetadata,
)
from app.connectors.http_limits import check_crawl_deadline, get_limited_http_response
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)

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
        self._robots_loaded = False
        self._robots_parser: RobotFileParser | None = None

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

    def _canonical_url(self, url: str) -> str:
        parts = urlparse(url)
        path = parts.path or "/"
        if path != "/":
            path = path.rstrip("/")
        return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, "", "", ""))

    def _record(
        self,
        url: str,
        *,
        state: str,
        reason_code: str,
        reason_detail: str | None = None,
        discovered_from: str | None = None,
        depth: int = 0,
        response: httpx.Response | None = None,
        final_url: str | None = None,
        canonical_url: str | None = None,
    ) -> None:
        self.record_discovery(
            DiscoveryObservation(
                url=url,
                state=state,  # type: ignore[arg-type]
                reason_code=reason_code,
                reason_detail=reason_detail,
                discovered_from=discovered_from,
                depth=depth,
                http_status=response.status_code if response is not None else None,
                content_type=response.headers.get("content-type") if response is not None else None,
                final_url=final_url,
                canonical_url=canonical_url,
            )
        )

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
            self._record(
                url,
                state="skipped",
                reason_code="cross_origin",
                reason_detail=str(e),
            )
            return False
        return True

    def _robots_allowed(self, url: str, *, discovered_from: str | None, depth: int) -> bool:
        if not self._robots_loaded:
            self._robots_loaded = True
            robots_url = urljoin(self.site.base_url.rstrip("/") + "/", "robots.txt")
            try:
                response = get_limited_http_response(
                    self.client,
                    robots_url,
                    max_bytes=settings.crawl_max_response_bytes,
                    crawl_started_at=self._crawl_started_at,
                )
            except httpx.HTTPError as error:
                self._record(
                    robots_url,
                    state="failed",
                    reason_code="robots_unavailable",
                    reason_detail=str(error),
                    discovered_from=discovered_from,
                    depth=depth,
                )
            else:
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(response.text.splitlines())
                    self._robots_parser = parser
                else:
                    self._record(
                        robots_url,
                        state="skipped",
                        reason_code="robots_unavailable",
                        reason_detail=f"HTTP {response.status_code}; crawl proceeds fail-open",
                        discovered_from=discovered_from,
                        depth=depth,
                        response=response,
                    )
        if self._robots_parser is None:
            return True
        allowed = self._robots_parser.can_fetch("LinkMesh/0.1", url)
        if not allowed:
            self._record(
                url,
                state="skipped",
                reason_code="robots_denied",
                discovered_from=discovered_from,
                depth=depth,
            )
        return allowed

    def _indexability(self, response: httpx.Response, tree) -> tuple[bool, str | None]:
        directives = [response.headers.get("x-robots-tag", "")]
        directives.extend(
            str(content)
            for content in tree.xpath(
                "//meta[translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz')='robots' or "
                "translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz')='googlebot']/@content"
            )
        )
        if any("noindex" in directive.lower() for directive in directives):
            return False, "noindex"
        return True, None

    def _canonical_from_tree(self, tree, *, page_url: str) -> tuple[str | None, str | None]:
        for link in tree.xpath("//link[@href]"):
            rel = {part.lower() for part in (link.get("rel") or "").split()}
            if "canonical" not in rel:
                continue
            canonical = self._canonical_url(urljoin(page_url, link.get("href")))
            if not self._same_origin(canonical):
                return None, "cross_origin_canonical"
            return canonical, None
        return None, None

    def _internal_links(self, tree, *, page_url: str) -> list[OutboundLink]:
        links: list[OutboundLink] = []
        for anchor in tree.xpath("//a[@href]"):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            absolute = self._canonical_url(urljoin(page_url, href))
            if not self._same_origin(absolute):
                continue
            links.append(
                OutboundLink(
                    url=absolute,
                    anchor_text=" ".join(anchor.text_content().split())[:_MAX_ANCHOR_TEXT_CHARS]
                    or None,
                )
            )
        if len(links) > settings.crawl_max_links_per_article:
            raise ValueError(f"article link count exceeded {settings.crawl_max_links_per_article}")
        return links

    def _fetch_page(
        self,
        url: str,
        *,
        discovered_from: str | None = None,
        depth: int = 0,
    ) -> tuple[ArticleData | None, list[OutboundLink]]:
        self._check_crawl_budget()
        try:
            validate_url(
                url,
                allow_private=settings.allow_unsafe_crawl_targets,
                same_host_as=self.site.base_url,
                resolve_dns=False,
            )
        except UnsafeURLError as error:
            self._record(
                url,
                state="failed",
                reason_code="unsafe_url",
                reason_detail=str(error),
                discovered_from=discovered_from,
                depth=depth,
            )
            return None, []
        try:
            response = get_limited_http_response(
                self.client,
                url,
                max_bytes=settings.crawl_max_response_bytes,
                crawl_started_at=self._crawl_started_at,
            )
        except httpx.HTTPError as error:
            self._record(
                url,
                state="failed",
                reason_code="http_error",
                reason_detail=str(error),
                discovered_from=discovered_from,
                depth=depth,
            )
            return None, []

        final_url = str(response.url)
        if not self._same_origin(final_url):
            self._record(
                url,
                state="failed",
                reason_code="cross_origin_redirect",
                reason_detail=f"redirected to {final_url}",
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
            )
            return None, []
        if response.status_code != 200:
            self._record(
                url,
                state="skipped",
                reason_code="http_error",
                reason_detail=f"HTTP {response.status_code}",
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
            )
            return None, []
        content_type = (response.headers.get("content-type") or "text/html").lower()
        if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
            self._record(
                url,
                state="skipped",
                reason_code="unsupported_content_type",
                reason_detail=content_type,
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
            )
            return None, []
        try:
            tree = lxml_html.fromstring(response.text)
        except (etree.XMLSyntaxError, ValueError) as error:
            self._record(
                url,
                state="failed",
                reason_code="invalid_html",
                reason_detail=str(error),
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
            )
            return None, []
        internal = self._internal_links(tree, page_url=final_url)
        indexable, indexability_reason = self._indexability(response, tree)
        if not indexable:
            self._record(
                url,
                state="skipped",
                reason_code=indexability_reason or "not_indexable",
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
            )
            return None, internal
        canonical_url, canonical_reason = self._canonical_from_tree(tree, page_url=final_url)
        if canonical_reason is not None:
            self._record(
                url,
                state="skipped",
                reason_code=canonical_reason,
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
            )
            return None, internal
        article_url = canonical_url or self._canonical_url(final_url)
        doc = trafilatura.bare_extraction(response.text, url=article_url, with_metadata=True)
        if doc is None or not doc.text:
            self._record(
                url,
                state="skipped",
                reason_code="not_article",
                discovered_from=discovered_from,
                depth=depth,
                response=response,
                final_url=final_url,
                canonical_url=canonical_url,
            )
            return None, internal
        if len(doc.text) > settings.crawl_max_article_chars:
            raise ValueError(
                f"article content exceeded {settings.crawl_max_article_chars} characters"
            )
        article = ArticleData(
            url=article_url,
            title=doc.title or article_url,
            content_text=doc.text,
            # Static export preparation needs the exact bounded source HTML.
            # It is never sent back to the site by this connector.
            content_html=response.text,
            language=None,
            published_at=None,
            taxonomies=[],
            outbound_internal_links=internal,
            discovered_from=discovered_from,
            discovery_depth=depth,
            canonical_url=canonical_url,
        )
        self._record(
            url,
            state="accepted",
            reason_code="accepted",
            discovered_from=discovered_from,
            depth=depth,
            response=response,
            final_url=final_url,
            canonical_url=canonical_url,
        )
        return article, internal

    def fetch_articles(self) -> Iterator[ArticleData]:
        self._crawl_started_at = monotonic()
        try:
            sitemap_urls = self._sitemap_urls()
        except (httpx.HTTPError, etree.XMLSyntaxError) as error:
            if not settings.crawl_bfs_fallback_enabled:
                raise
            index_url = self.site.base_url.rstrip("/") + "/sitemap_index.xml"
            self._record(
                index_url,
                state="skipped",
                reason_code="sitemap_unavailable",
                reason_detail=str(error),
            )
            sitemap_urls = []
        if sitemap_urls:
            article_number = 0
            for url in sitemap_urls:
                if not self._robots_allowed(
                    url,
                    discovered_from=self.site.base_url.rstrip("/") + "/sitemap_index.xml",
                    depth=0,
                ):
                    continue
                article, _ = self._fetch_page(
                    url,
                    discovered_from=self.site.base_url.rstrip("/") + "/sitemap_index.xml",
                    depth=0,
                )
                if article is None:
                    continue
                article_number += 1
                if article_number > settings.crawl_max_articles:
                    raise ValueError(f"crawl article count exceeded {settings.crawl_max_articles}")
                yield article
            return
        if not settings.crawl_bfs_fallback_enabled:
            return
        self._record(
            self.site.base_url,
            state="skipped",
            reason_code="sitemap_empty",
        )
        yield from self._fetch_bfs_articles()

    def _fetch_bfs_articles(self) -> Iterator[ArticleData]:
        queue = deque([(self._canonical_url(self.site.base_url), None, 0)])
        queued = {queue[0][0]}
        seen: set[str] = set()
        articles = 0
        while queue:
            url, discovered_from, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if depth > settings.crawl_max_depth:
                self._record(
                    url,
                    state="skipped",
                    reason_code="max_depth",
                    discovered_from=discovered_from,
                    depth=depth,
                )
                continue
            if not self._robots_allowed(url, discovered_from=discovered_from, depth=depth):
                continue
            article, internal = self._fetch_page(
                url,
                discovered_from=discovered_from,
                depth=depth,
            )
            if article is not None:
                articles += 1
                if articles > settings.crawl_max_articles:
                    raise ValueError(f"crawl article count exceeded {settings.crawl_max_articles}")
                yield article
            if depth >= settings.crawl_max_depth:
                continue
            for link in internal:
                candidate = self._canonical_url(link.url)
                if candidate in seen or candidate in queued:
                    continue
                if len(seen) + len(queue) + 1 > settings.crawl_max_discovered_urls:
                    self._record(
                        candidate,
                        state="skipped",
                        reason_code="max_discovered_urls",
                        discovered_from=url,
                        depth=depth + 1,
                    )
                    continue
                queued.add(candidate)
                queue.append((candidate, url, depth + 1))

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        self._crawl_started_at = self._crawl_started_at or monotonic()
        if not self._robots_allowed(url, discovered_from=None, depth=0):
            return None
        article, _ = self._fetch_page(url)
        return article

    def get_site_metadata(self) -> SiteMetadata:
        try:
            article_count = len(self._sitemap_urls())
        except httpx.HTTPError:
            article_count = None
        return SiteMetadata(
            name=self.site.name,
            base_url=self.site.base_url,
            platform="html",
            article_count=article_count,
        )

    def supports_incremental_sync(self) -> bool:
        return False

    def apply_planned_edit(
        self, source, *, original_html: str, updated_html: str
    ) -> PlannedEditOutcome:
        # A3 resolved: design ready (FTP hypothesis documented), no implementation —
        # HTML sites are secondary, WordPress is the v1 priority.
        raise NotImplementedError("writing to static HTML sites is not supported in v1 (A3)")

    def preview_links(self, suggestions) -> LinkPreview:
        """Render exact bytes for a non-writing static-site export."""
        if not suggestions:
            return LinkPreview("", "", [])
        source = suggestions[0].source_article
        content = source.content_html
        if not content:
            raise ValueError(
                f"article {source.id} has no stored HTML; re-ingest the static site before preparing"
            )
        original = content
        outcomes: list[LinkOutcome] = []
        for suggestion in suggestions:
            target_url = suggestion.resolved_target_url
            tree = lxml_html.fromstring(content)
            existing = {urljoin(source.url, href) for href in tree.xpath("//a/@href") if href}
            if target_url in existing:
                outcomes.append("already_present")
                continue
            anchor = suggestion.anchor_text or suggestion.resolved_target_title
            start = content.find(anchor) if anchor else -1
            open_tag = (
                max(content.rfind("<a ", 0, start), content.rfind("<a>", 0, start))
                if start >= 0
                else -1
            )
            close_tag = content.rfind("</a>", 0, start) if start >= 0 else -1
            if start >= 0 and open_tag <= close_tag:
                href = escape(target_url, quote=True)
                content = (
                    f'{content[:start]}<a href="{href}">{content[start : start + len(anchor)]}</a>'
                    f"{content[start + len(anchor) :]}"
                )
                outcomes.append("inserted")
                continue
            href = escape(target_url, quote=True)
            label = escape(anchor or target_url)
            content += f'\n<p>Read also: <a href="{href}">{label}</a></p>'
            outcomes.append("block")
        return LinkPreview(original, content, outcomes)
