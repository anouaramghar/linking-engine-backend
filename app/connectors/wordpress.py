"""WordPress connector — WP REST API, read via public API, write via Application Passwords (A2)."""

import html
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx
from lxml import etree, html as lxml_html
from lxml.etree import ParserError
from lxml.html import HtmlElement

from app.config import settings
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
from app.connectors.http_limits import check_crawl_deadline, get_limited_http_response
from app.connectors.url_guard import SSRFProtectedTransport, request_guard, validate_url
from app.models.suggestion import Suggestion

_API_DISCOVERY_REL = "https://api.w.org/"
_API_LINK_RE = re.compile(r"<([^>]+)>;\s*rel=[\"']https://api\.w\.org/[\"']", re.IGNORECASE)
_FEED_MEDIA_TYPES = {
    "application/atom+xml",
    "application/rdf+xml",
    "application/rss+xml",
    "application/x-rss+xml",
    "application/xml",
    "text/xml",
}
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)
logger = logging.getLogger(__name__)


def _iso(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


def _parse_html(fragment: str) -> HtmlElement | None:
    if not fragment.strip():
        return None
    try:
        return lxml_html.fromstring(fragment)
    except ParserError:
        return None


def _text_content(tree: HtmlElement | None) -> str:
    return tree.text_content().strip() if tree is not None else ""


def _strip_html(fragment: str) -> str:
    return _text_content(_parse_html(fragment))


def _safe_join(page_url: str, href: str) -> str | None:
    try:
        return urljoin(page_url, href)
    except ValueError:
        logger.warning("Skipping malformed WordPress href on %r: %r", page_url, href[:200])
        return None


class WordPressConnector(ContentConnector):
    def __init__(self, site):
        super().__init__(site)
        has_username = bool(site.wp_username)
        has_password = bool(site.wp_app_password)
        if has_username != has_password:
            raise ValueError("wp_username and wp_app_password must be provided together")
        auth = (site.wp_username, site.wp_app_password) if has_username else None
        allow_private = settings.allow_unsafe_crawl_targets
        require_https = auth is not None and not allow_private
        # Fail fast on scheme/credential problems. The request hook repeats those
        # checks per redirect; the transport validates and pins the connected IP.
        validate_url(
            site.base_url,
            allow_private=allow_private,
            require_https=require_https,
            resolve_dns=False,
        )
        self._auth = auth
        self._credential_origin = self._origin(site.base_url) if auth else None
        self.client = httpx.Client(
            transport=SSRFProtectedTransport(allow_private=allow_private),
            trust_env=False,
            base_url=site.base_url.rstrip("/"),
            timeout=30,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "LinkMesh/0.1"},
            event_hooks={
                "request": [request_guard(allow_private=allow_private, require_https=require_https)]
            },
        )
        self._host = urlparse(site.base_url).netloc
        self._site_path = urlparse(site.base_url).path.rstrip("/")
        self._content_hosts = {self._host.lower()}
        self._canonical_host: str | None = None
        self._canonical_url_cache: dict[str, str] = {}
        self._api_candidates: list[str] | None = None
        self._api_base_url: str | None = None
        self._crawl_started_at: float | None = None

    # -- reading ---------------------------------------------------------

    @staticmethod
    def _resource(path: str) -> str:
        return path.removeprefix("/wp-json/wp/v2/").lstrip("/")

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        parts = urlparse(url)
        return (
            parts.scheme.lower(),
            (parts.hostname or "").lower(),
            parts.port or (443 if parts.scheme.lower() == "https" else 80),
        )

    def _request_kwargs(self, url: str, **kwargs):
        if self._auth is not None and self._origin(url) == self._credential_origin:
            kwargs["auth"] = self._auth
        return kwargs

    def _get(self, url: str, **kwargs) -> httpx.Response:
        if self._crawl_started_at is None:
            self._crawl_started_at = monotonic()
        check_crawl_deadline(self._crawl_started_at)
        return get_limited_http_response(
            self.client,
            url,
            max_bytes=settings.crawl_max_response_bytes,
            **kwargs,
        )

    @staticmethod
    def _looks_like_json(response: httpx.Response) -> bool:
        if response.status_code == 404:
            return False
        media_type = (response.headers.get("content-type") or "").split(";", 1)[0]
        return media_type.lower() == "application/json" or response.content.lstrip().startswith(
            (b"{", b"[")
        )

    @staticmethod
    def _api_v2_base(api_root: str) -> str:
        parts = urlparse(api_root)
        query = parse_qsl(parts.query, keep_blank_values=True)
        for index, (key, value) in enumerate(query):
            if key == "rest_route":
                route = value.rstrip("/")
                if not route:
                    route = "/wp/v2"
                elif not route.endswith("/wp/v2"):
                    route += "/wp/v2"
                query[index] = (key, route + "/")
                return parts._replace(query=urlencode(query, safe="/")).geturl()
        root = api_root.rstrip("/") + "/"
        return root if root.rstrip("/").endswith("/wp/v2") else urljoin(root, "wp/v2/")

    @staticmethod
    def _api_url(base_url: str, resource: str) -> str:
        parts = urlparse(base_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        for index, (key, value) in enumerate(query):
            if key == "rest_route":
                route = value or "/"
                if resource:
                    route = f"{route.rstrip('/')}/{resource}"
                elif not route.endswith("/"):
                    route += "/"
                query[index] = (key, route)
                return parts._replace(query=urlencode(query, safe="/")).geturl()
        return urljoin(base_url, resource)

    @staticmethod
    def _with_params(url: str, params: dict | None) -> str:
        if not params:
            return url
        parts = urlparse(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.extend(params.items())
        return parts._replace(query=urlencode(query, doseq=True, safe="/")).geturl()

    def _add_api_candidate(self, candidates: list[str], url: str) -> None:
        parts = urlparse(url)
        has_rest_route = any(
            key == "rest_route" for key, _value in parse_qsl(parts.query, keep_blank_values=True)
        )
        candidate = (
            url
            if has_rest_route
            else parts._replace(path=parts.path.rstrip("/") + "/").geturl()
        )
        host = urlparse(candidate).netloc.lower()
        if host not in self._content_hosts and host != "public-api.wordpress.com":
            return
        if candidate not in candidates:
            candidates.append(candidate)

    def _discover_page_links(self) -> tuple[list[str], list[str]]:
        """Find WordPress API/feed links when the submitted URL is a page URL."""
        try:
            response = self._get(
                self.site.base_url, **self._request_kwargs(self.site.base_url)
            )
        except httpx.HTTPError:
            return [], []

        response_host = urlparse(str(response.url)).netloc.lower()
        if response_host:
            self._content_hosts.add(response_host)

        api_roots = [
            match.group(1) for match in _API_LINK_RE.finditer(response.headers.get("link", ""))
        ]
        feed_urls: list[str] = []
        tree = _parse_html(response.text)
        if tree is None:
            return api_roots, feed_urls

        for link in tree.xpath("//link[@href]"):
            href = link.get("href")
            if not href:
                continue
            absolute = _safe_join(str(response.url), href)
            if absolute is None:
                continue
            rel = (link.get("rel") or "").lower()
            media_type = (link.get("type") or "").split(";", 1)[0].lower()
            if _API_DISCOVERY_REL in rel:
                api_roots.append(absolute)
            elif "alternate" in rel.split() and media_type in _FEED_MEDIA_TYPES:
                feed_urls.append(absolute)
        return api_roots, feed_urls

    @staticmethod
    def _feed_site_id(content: bytes) -> str | None:
        try:
            root = etree.fromstring(content, parser=_XML_PARSER)
        except (ValueError, etree.XMLSyntaxError):
            return None
        for element in root.iter():
            if isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1] == "site":
                site_id = (element.text or "").strip()
                if site_id.isdigit():
                    return site_id
        return None

    def _wordpress_com_api(self, feed_urls: list[str]) -> str | None:
        if (urlparse(self.site.base_url).hostname or "").lower() != "wordpress.com":
            return None

        parts = urlparse(self.site.base_url)
        path = parts.path.rstrip("/")
        paths: list[str] = []
        while True:
            paths.append(path)
            if not path:
                break
            path = path.rsplit("/", 1)[0]
        origin = f"{parts.scheme}://{parts.netloc}"
        feed_urls += [f"{origin}{path}/feed/" for path in paths]

        seen: set[str] = set()
        for feed_url in feed_urls:
            feed_host = urlparse(feed_url).netloc.lower()
            if feed_host not in self._content_hosts or feed_url in seen:
                continue
            seen.add(feed_url)
            try:
                response = self._get(feed_url, **self._request_kwargs(feed_url))
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            site_id = self._feed_site_id(response.content)
            if site_id:
                return f"https://public-api.wordpress.com/wp/v2/sites/{site_id}/"
        return None

    def _prepare_api_candidates(self) -> None:
        if self._api_candidates is not None:
            return

        parts = urlparse(self.site.base_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")
        api_roots, feed_urls = self._discover_page_links()
        candidates: list[str] = []

        if wordpress_com_api := self._wordpress_com_api(feed_urls):
            self._add_api_candidate(candidates, wordpress_com_api)
        for api_root in api_roots:
            self._add_api_candidate(candidates, self._api_v2_base(api_root))

        # Keep the submitted path and every parent as fallbacks for sites
        # installed in a subdirectory, then try the host root.
        paths: list[str] = []
        while True:
            paths.append(path)
            if not path:
                break
            path = path.rsplit("/", 1)[0]
        for path_prefix in paths:
            self._add_api_candidate(candidates, f"{origin}{path_prefix}/wp-json/wp/v2/")
        self._api_candidates = candidates

    def _api_get(self, path: str, params: dict | None = None) -> httpx.Response:
        self._prepare_api_candidates()
        candidates = [self._api_base_url] if self._api_base_url else self._api_candidates or []
        last_response: httpx.Response | None = None
        for base_url in candidates:
            url = self._with_params(self._api_url(base_url, self._resource(path)), params)
            response = self._get(url, **self._request_kwargs(url))
            last_response = response
            if self._api_base_url is not None or self._looks_like_json(response):
                self._api_base_url = base_url
                return response

        if last_response is None:
            raise ValueError(f"no WordPress API candidate found for {self.site.base_url}")
        if last_response.status_code >= 400:
            raise ValueError(
                f"WordPress API was not found for {self.site.base_url}; "
                f"last response was HTTP {last_response.status_code} from {last_response.url}"
            )
        media_type = (last_response.headers.get("content-type") or "unknown").split(";", 1)[0]
        raise ValueError(
            f"WordPress API returned {media_type} from {last_response.url}, expected JSON"
        )

    @staticmethod
    def _json(response: httpx.Response):
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as error:
            media_type = (response.headers.get("content-type") or "unknown").split(";", 1)[0]
            raise ValueError(
                f"WordPress API returned {media_type} from {response.url}, expected JSON"
            ) from error

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 1
        while page <= settings.crawl_max_wordpress_pages:
            resp = self._api_get(path, params={"per_page": 100, "page": page, **(params or {})})
            if resp.status_code == 400:  # WP returns 400 past the last page
                return
            items = self._json(resp)
            if not items:
                return
            yield from items
            total_pages = resp.headers.get("X-WP-TotalPages")
            if total_pages is not None:
                declared_total = int(total_pages)
                if declared_total > settings.crawl_max_wordpress_pages:
                    raise ValueError(
                        "WordPress pagination exceeded the configured page limit"
                    )
                if page >= declared_total:
                    return
            elif page >= settings.crawl_max_wordpress_pages:
                raise ValueError("WordPress pagination exceeded the configured page limit")
            page += 1
        raise ValueError("WordPress pagination exceeded the configured page limit")

    def _taxonomy_map(self) -> dict[tuple[str, int], TaxonomyData]:
        """Map (kind, WP term id) to taxonomy; category and tag ids can overlap."""
        out: dict[tuple[str, int], TaxonomyData] = {}
        for path, kind in [
            ("categories", "category"),
            ("tags", "tag"),
        ]:
            for term in self._paginate(path):
                out[(kind, term["id"])] = TaxonomyData(
                    kind=kind,
                    name=term["name"],
                    external_id=str(term["id"]),
                )
        return out

    def _internal_hrefs(self, content_html: str, page_url: str) -> list[str]:
        """Hrefs pointing at this site's host, resolved absolute (handles /relative/ links)."""
        return self._internal_hrefs_from_tree(_parse_html(content_html), page_url)

    def _all_hrefs(self, content_html: str, page_url: str) -> list[str]:
        """Every href on the page, resolved absolute, whatever host it points at.

        The publication idempotency check needs this rather than `_internal_hrefs`:
        a content-pool target is external by definition, so filtering to this
        site's host would drop the very link we are checking for and append a
        duplicate "Read also" block on every retry.
        """
        tree = _parse_html(content_html)
        if tree is None:
            return []
        return [
            absolute
            for href in tree.xpath("//a/@href")
            if (absolute := _safe_join(page_url, href)) is not None
        ]

    def _is_internal_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host == self._canonical_host:
            return True
        if host != self._host.lower():
            return False
        if self._host.lower() == "wordpress.com" and self._site_path:
            return parsed.path == self._site_path or parsed.path.startswith(self._site_path + "/")
        return True

    def _canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        if (
            self._host.lower() == "wordpress.com"
            and self._canonical_host
            and parsed.netloc.lower() == self._host.lower()
            and self._site_path
            and (parsed.path == self._site_path or parsed.path.startswith(self._site_path + "/"))
        ):
            path = parsed.path[len(self._site_path) :] or "/"
            return parsed._replace(netloc=self._canonical_host, path=path).geturl()
        return url

    def _internal_hrefs_from_tree(self, tree: HtmlElement | None, page_url: str) -> list[str]:
        if tree is None:
            return []
        hrefs = []
        for href in tree.xpath("//a/@href"):
            joined = _safe_join(page_url, href)
            if joined is None:
                continue
            absolute = self._canonicalize_url(joined)
            if self._is_internal_url(absolute):
                hrefs.append(absolute)
        return hrefs

    def resolve_internal_url(self, url: str) -> str:
        """Resolve redirect aliases without changing external-origin policy."""
        cached = self._canonical_url_cache.get(url)
        if cached is not None:
            return cached
        try:
            response = self.client.head(url, **self._request_kwargs(url))
            if response.status_code in {405, 501}:
                response = self._get(url, **self._request_kwargs(url))
            canonical = str(response.url)
        except httpx.HTTPError:
            return url
        if not self._is_internal_url(canonical):
            return url
        self._canonical_url_cache[url] = canonical
        return canonical

    def _to_article(
        self, post: dict, taxonomy_map: dict[tuple[str, int], TaxonomyData]
    ) -> ArticleData:
        post_host = urlparse(post["link"]).netloc.lower()
        if (
            self._host.lower() == "wordpress.com"
            and post_host.endswith(".wordpress.com")
        ):
            self._content_hosts.add(post_host)
            self._canonical_host = post_host
        content_html = post["content"]["rendered"]
        content_tree = _parse_html(content_html)
        content_text = _text_content(content_tree)
        if len(content_text) > settings.crawl_max_article_chars:
            raise ValueError(
                f"article content exceeded {settings.crawl_max_article_chars} characters"
            )
        internal_urls = self._internal_hrefs_from_tree(content_tree, post["link"])
        if len(internal_urls) > settings.crawl_max_links_per_article:
            raise ValueError(
                f"article link count exceeded {settings.crawl_max_links_per_article}"
            )
        term_refs = [
            *(("category", term_id) for term_id in post.get("categories", [])),
            *(("tag", term_id) for term_id in post.get("tags", [])),
        ]
        return ArticleData(
            url=post["link"],
            external_id=str(post["id"]),
            title=_strip_html(post["title"]["rendered"]),
            content_text=content_text,
            content_html=content_html,
            language=None,  # language filter disabled (A5) — fleet is 100% English
            published_at=_iso(post.get("date_gmt")),
            taxonomies=[taxonomy_map[ref] for ref in term_refs if ref in taxonomy_map],
            outbound_internal_urls=internal_urls,
        )

    def fetch_articles(self) -> Iterator[ArticleData]:
        taxonomy_map = self._taxonomy_map()
        for article_number, post in enumerate(
            self._paginate("posts", {"status": "publish"}),
            start=1,
        ):
            if article_number > settings.crawl_max_articles:
                raise ValueError(f"crawl article count exceeded {settings.crawl_max_articles}")
            yield self._to_article(post, taxonomy_map)

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        posts = self._json(self._api_get("posts", params={"slug": slug}))
        return self._to_article(posts[0], self._taxonomy_map()) if posts else None

    def get_site_metadata(self) -> SiteMetadata:
        root = self._json(self._api_get(""))
        head = self._api_get("posts", params={"per_page": 1})
        return SiteMetadata(
            name=root.get("name", self.site.name),
            base_url=self.site.base_url,
            platform="wordpress",
            article_count=int(head.headers.get("X-WP-Total", 0)),
        )

    def supports_incremental_sync(self) -> bool:
        return True  # WP REST supports ?modified_after — used from v2 re-crawl policy

    # -- writing (publication worker) --------------------------------------

    def apply_link(self, suggestion: Suggestion) -> None:
        source = suggestion.source_article
        target = suggestion.target_article
        if not source.external_id:
            raise ValueError(f"article {source.id} has no WP post id")
        resp = self._api_get(f"posts/{source.external_id}", params={"context": "edit"})
        content = self._json(resp)["content"]["raw"]
        if content.strip():
            # exact href match, not substring — "/post" must not match href="/post-2".
            # Every href, not just internal ones: the retry-safety this gives the
            # publication worker has to hold for external content-pool targets too.
            existing = set(self._all_hrefs(content, source.url))
            if target.url in existing:
                return  # link already present — idempotent
        # ponytail: appended "read also" block; in-text placement + anchor generation is v4
        anchor = html.escape(suggestion.anchor_text or target.title)
        content += f'\n<p>Read also: <a href="{html.escape(target.url)}">{anchor}</a></p>'
        url = self._api_url(self._api_base_url or "", f"posts/{source.external_id}")
        update = self.client.post(
            url,
            json={"content": content},
            **self._request_kwargs(url),
        )
        update.raise_for_status()
