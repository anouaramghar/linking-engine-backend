"""WordPress connector — WP REST API, read via public API, write via Application Passwords (A2)."""

import html
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as lxml_html
from lxml.etree import ParserError
from lxml.html import HtmlElement

from app.config import settings
from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
from app.connectors.url_guard import SSRFProtectedTransport, request_guard, validate_url
from app.models.suggestion import Suggestion


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
        self.client = httpx.Client(
            transport=SSRFProtectedTransport(allow_private=allow_private),
            trust_env=False,
            base_url=site.base_url.rstrip("/"),
            auth=auth,
            timeout=30,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "LinkMesh/0.1"},
            event_hooks={
                "request": [request_guard(allow_private=allow_private, require_https=require_https)]
            },
        )
        self._host = urlparse(site.base_url).netloc

    # -- reading ---------------------------------------------------------

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 1
        while True:
            resp = self.client.get(path, params={"per_page": 100, "page": page, **(params or {})})
            if resp.status_code == 400:  # WP returns 400 past the last page
                return
            resp.raise_for_status()
            items = resp.json()
            if not items:
                return
            yield from items
            total_pages = resp.headers.get("X-WP-TotalPages")
            if total_pages is not None and page >= int(total_pages):
                return
            page += 1

    def _taxonomy_map(self) -> dict[tuple[str, int], TaxonomyData]:
        """Map (kind, WP term id) to taxonomy; category and tag ids can overlap."""
        out: dict[tuple[str, int], TaxonomyData] = {}
        for path, kind in [("/wp-json/wp/v2/categories", "category"), ("/wp-json/wp/v2/tags", "tag")]:
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

    def _internal_hrefs_from_tree(
        self, tree: HtmlElement | None, page_url: str
    ) -> list[str]:
        if tree is None:
            return []
        hrefs = []
        for href in tree.xpath("//a/@href"):
            absolute = urljoin(page_url, href)
            if urlparse(absolute).netloc == self._host:
                hrefs.append(absolute)
        return hrefs

    def _to_article(
        self, post: dict, taxonomy_map: dict[tuple[str, int], TaxonomyData]
    ) -> ArticleData:
        content_html = post["content"]["rendered"]
        content_tree = _parse_html(content_html)
        term_refs = [
            *(("category", term_id) for term_id in post.get("categories", [])),
            *(("tag", term_id) for term_id in post.get("tags", [])),
        ]
        return ArticleData(
            url=post["link"],
            external_id=str(post["id"]),
            title=_strip_html(post["title"]["rendered"]),
            content_text=_text_content(content_tree),
            content_html=content_html,
            language=None,  # language filter disabled (A5) — fleet is 100% English
            published_at=_iso(post.get("date_gmt")),
            taxonomies=[taxonomy_map[ref] for ref in term_refs if ref in taxonomy_map],
            outbound_internal_urls=self._internal_hrefs_from_tree(content_tree, post["link"]),
        )

    def fetch_articles(self) -> Iterator[ArticleData]:
        taxonomy_map = self._taxonomy_map()
        for post in self._paginate("/wp-json/wp/v2/posts", {"status": "publish"}):
            yield self._to_article(post, taxonomy_map)

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        resp = self.client.get("/wp-json/wp/v2/posts", params={"slug": slug})
        resp.raise_for_status()
        posts = resp.json()
        return self._to_article(posts[0], self._taxonomy_map()) if posts else None

    def get_site_metadata(self) -> SiteMetadata:
        root = self.client.get("/wp-json").json()
        head = self.client.get("/wp-json/wp/v2/posts", params={"per_page": 1})
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
        resp = self.client.get(f"/wp-json/wp/v2/posts/{source.external_id}", params={"context": "edit"})
        resp.raise_for_status()
        content = resp.json()["content"]["raw"]
        if content.strip():
            # exact href match, not substring — "/post" must not match href="/post-2"
            existing = set(self._internal_hrefs(content, source.url))
            if target.url in existing:
                return  # link already present — idempotent
        # ponytail: appended "read also" block; in-text placement + anchor generation is v4
        anchor = html.escape(suggestion.anchor_text or target.title)
        content += f'\n<p>Read also: <a href="{html.escape(target.url)}">{anchor}</a></p>'
        update = self.client.post(
            f"/wp-json/wp/v2/posts/{source.external_id}", json={"content": content}
        )
        update.raise_for_status()
