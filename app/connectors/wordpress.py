"""WordPress connector — WP REST API, read via public API, write via Application Passwords (A2)."""

from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from lxml import html as lxml_html

from app.connectors.base import ArticleData, ContentConnector, SiteMetadata, TaxonomyData
from app.models.suggestion import Suggestion


def _iso(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


def _strip_html(fragment: str) -> str:
    if not fragment.strip():
        return ""
    return lxml_html.fromstring(fragment).text_content().strip()


class WordPressConnector(ContentConnector):
    def __init__(self, site):
        super().__init__(site)
        auth = (site.wp_username, site.wp_app_password) if site.wp_username else None
        self.client = httpx.Client(
            base_url=site.base_url.rstrip("/"),
            auth=auth,
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "LinkMesh/0.1"},
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
            if page >= int(resp.headers.get("X-WP-TotalPages", page)):
                return
            page += 1

    def _taxonomy_map(self) -> dict[int, TaxonomyData]:
        """WP term id -> TaxonomyData, for categories and tags (A14 — free from the API)."""
        out: dict[int, TaxonomyData] = {}
        for path, kind in [("/wp-json/wp/v2/categories", "category"), ("/wp-json/wp/v2/tags", "tag")]:
            for term in self._paginate(path):
                out[term["id"]] = TaxonomyData(kind=kind, name=term["name"], external_id=str(term["id"]))
        return out

    def _internal_hrefs(self, content_html: str) -> list[str]:
        if not content_html.strip():
            return []
        tree = lxml_html.fromstring(content_html)
        return [
            href
            for href in tree.xpath("//a/@href")
            if urlparse(href).netloc == self._host
        ]

    def _to_article(self, post: dict, taxonomy_map: dict[int, TaxonomyData]) -> ArticleData:
        content_html = post["content"]["rendered"]
        term_ids = post.get("categories", []) + post.get("tags", [])
        return ArticleData(
            url=post["link"],
            external_id=str(post["id"]),
            title=_strip_html(post["title"]["rendered"]),
            content_text=_strip_html(content_html),
            content_html=content_html,
            language=None,  # language filter disabled (A5) — fleet is 100% English
            published_at=_iso(post.get("date_gmt")),
            taxonomies=[taxonomy_map[tid] for tid in term_ids if tid in taxonomy_map],
            outbound_internal_urls=self._internal_hrefs(content_html),
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
        if target.url in content:
            return  # link already present — idempotent
        # ponytail: appended "read also" block; in-text placement + anchor generation is v4
        anchor = suggestion.anchor_text or target.title
        content += f'\n<p>Read also: <a href="{target.url}">{anchor}</a></p>'
        update = self.client.post(
            f"/wp-json/wp/v2/posts/{source.external_id}", json={"content": content}
        )
        update.raise_for_status()
