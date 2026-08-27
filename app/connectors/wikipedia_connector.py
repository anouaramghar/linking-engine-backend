"""Read-only Wikipedia content-pool connector using the MediaWiki API."""

import json
import time
from collections.abc import Iterator, Sequence
from datetime import datetime
from urllib.parse import unquote, urlparse

import httpx

from app.config import settings
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    OutboundLink,
    PlannedEditOutcome,
    SiteMetadata,
    TaxonomyData,
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


#: Slack added to the request budget for one article listing. MediaWiki returns
#: a single extract per query, so the budget is one request per requested
#: article plus room for search hits that never carry an extract at all.
PAGE_REQUEST_SLACK = 20

#: Consecutive *empty* responses allowed before the listing is treated as a
#: looping pager rather than a short result set.
#:
#: Deliberately not "requests that completed no article". MediaWiki carries
#: several continuations at once -- one per requested property -- and spends
#: whole requests advancing `info` or `revisions` without delivering a new
#: extract. Those look identical to a stall and are not one. A pager that is
#: genuinely stuck returns no pages at all, which is what this counts.
MAX_EMPTY_RESPONSES = 3

#: Page ids per link query. MediaWiki accepts 50 titles or ids per request for
#: a regular user account.
LINK_QUERY_BATCH = 50

#: Continuation requests allowed per batch. MediaWiki returns at most 500 links
#: per query, so this bounds one batch well above the per-article cap before the
#: connector refuses to keep paging.
LINK_QUERY_MAX_REQUESTS = 50


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

    def _payload(self, params: dict) -> dict:
        """Return one validated API response body."""
        response = get_limited_response(
            self.client,
            "/w/api.php",
            params=params,
            max_bytes=settings.pool_max_response_bytes,
        )
        media_type = (response.content_type or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise ValueError(
                f"Wikipedia API returned unsupported Content-Type {media_type or 'missing'}"
            )
        try:
            payload = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Wikipedia API returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise TypeError("Wikipedia API returned an invalid payload")
        api_error = payload.get("error")
        if api_error is not None:
            info = api_error.get("info", "unknown") if isinstance(api_error, dict) else api_error
            raise ValueError(f"Wikipedia API error: {info}")
        return payload

    def _pages(self, *, result_limit: int | None = None, **query) -> list[dict]:
        """Return pages that carry an extract, following MediaWiki continuation.

        Accumulated by page id rather than appended. A generator query repeats
        the same page stubs on every continuation and fills in a different
        subset of the expensive properties each time, so appending would return
        the same article many times over, most copies without content.

        One request yields one extract. MediaWiki enforces `exlimit=1` whenever
        `exintro` is unset, and full article text is what the ranker reads, so
        several requests are needed to fill a page of search results. Before
        this accounting, the search generator returned twenty stubs, the loop
        counted them as twenty results, and the source produced exactly one
        article per crawl.

        Termination is by progress rather than by an article count. Some search
        hits never yield an extract at all, so a budget of one request per
        requested article stops early on a corpus that simply contains a few
        such pages.
        """
        pages: dict[str, dict] = {}
        continuation: dict = {}
        max_requests = 1
        if result_limit:
            max_requests = result_limit * 2 + PAGE_REQUEST_SLACK
        empty_responses = 0

        def _complete() -> list[dict]:
            return [page for page in pages.values() if str(page.get("extract") or "").strip()]

        for request_number in range(max_requests):
            params = {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "redirects": 1,
                "prop": "extracts|info|revisions",
                "inprop": "url",
                "explaintext": 1,
                "rvprop": "timestamp",
                **query,
                **continuation,
            }
            # MediaWiki rejects rvlimit when a generator supplies several
            # pages. A direct title lookup is a single-page query and may pin
            # the revision count explicitly; generator queries already return
            # the latest revision by default.
            if "generator" not in query:
                params["rvlimit"] = 1
            payload = self._payload(params)
            returned = payload.get("query", {}).get("pages", [])
            empty_responses = 0 if returned else empty_responses + 1
            if empty_responses >= MAX_EMPTY_RESPONSES:
                raise ValueError("Wikipedia API pagination exceeded request limit")
            for page in returned:
                key = str(page.get("pageid") or page.get("title") or "")
                merged = {**pages.get(key, {}), **page}
                # A later continuation reports the same stub without the extract
                # it already sent. Keeping the value already held stops a
                # complete article being emptied by its own follow-up page.
                if not str(page.get("extract") or "").strip():
                    existing = str(pages.get(key, {}).get("extract") or "")
                    if existing:
                        merged["extract"] = existing
                pages[key] = merged
            if result_limit is not None and len(_complete()) >= result_limit:
                return _complete()[:result_limit]
            continuation = payload.get("continue") or {}
            # A direct-title lookup pins revisions to one item (`rvlimit=1`).
            # MediaWiki still advertises older revisions through `rvcontinue`,
            # but they cannot change the current article we are validating.
            if not continuation or result_limit is None:
                return list(pages.values())
            if request_number + 1 >= max_requests:
                raise ValueError("Wikipedia API pagination exceeded request limit")
            time.sleep(settings.pool_request_delay_seconds)
        return _complete()[:result_limit] if result_limit else list(pages.values())

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
            outbound_internal_links=[],
        )

    def _fetch_creation_dates(self, page_ids: Sequence[int]) -> dict[int, datetime]:
        """Return the timestamp of each page's first revision.

        `prop=revisions` without a direction reports the *latest* edit, which
        makes every encyclopedia article look as if it were published today.
        Ranking evaluation splits sources from targets by publication date, so
        that reading collapses a twenty-year corpus into a single instant and
        leaves no measurable "new source, old target" pair.

        MediaWiki refuses `rvlimit` when a query names more than one page, so
        the first revision costs one request per article. The politeness delay
        between them is the same one the rest of this connector uses.
        """
        created: dict[int, datetime] = {}
        for index, page_id in enumerate(page_ids):
            payload = self._payload(
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": 2,
                    "pageids": str(page_id),
                    "prop": "revisions",
                    "rvprop": "timestamp",
                    "rvdir": "newer",
                    "rvlimit": 1,
                }
            )
            for page in payload.get("query", {}).get("pages", []):
                revisions = page.get("revisions") or []
                if revisions and (stamp := _timestamp(revisions[0].get("timestamp"))):
                    created[int(page.get("pageid", page_id))] = stamp
            if index + 1 < len(page_ids):
                time.sleep(settings.pool_request_delay_seconds)
        return created

    def _fetch_links(self, page_ids: Sequence[int]) -> dict[int, list[str]]:
        """Return the article-namespace links of each page, keyed by page id.

        Fetched in its own pass rather than alongside the extracts. MediaWiki
        applies one shared link budget per query (`limits: {"links": 500}` in the
        response), so asking for links and extracts together makes the number of
        links a page reports depend on how many other pages shared the request.
        A dedicated query keeps each page's link set a property of that page.

        Only namespace 0 is requested, so categories, templates and talk pages
        never enter the link graph.

        Returns link *titles*. The caller resolves them against the titles it
        actually fetched, so no URL is ever reconstructed: MediaWiki's canonical
        `fullurl` leaves characters such as parentheses unescaped, and a rebuilt
        URL would silently fail to match it.
        """
        if not page_ids or settings.pool_max_links_per_article <= 0:
            return {}
        links: dict[int, list[str]] = {}
        for batch_start in range(0, len(page_ids), LINK_QUERY_BATCH):
            batch = page_ids[batch_start : batch_start + LINK_QUERY_BATCH]
            continuation: dict = {}
            for _ in range(LINK_QUERY_MAX_REQUESTS):
                payload = self._payload(
                    {
                        "action": "query",
                        "format": "json",
                        "formatversion": 2,
                        "pageids": "|".join(str(page_id) for page_id in batch),
                        "prop": "links",
                        "plnamespace": 0,
                        "pllimit": "max",
                        **continuation,
                    }
                )
                for page in payload.get("query", {}).get("pages", []):
                    page_id = page.get("pageid")
                    if page_id is None:
                        continue
                    collected = links.setdefault(int(page_id), [])
                    for link in page.get("links") or []:
                        if len(collected) >= settings.pool_max_links_per_article:
                            break
                        title = str(link.get("title") or "").strip()
                        if title:
                            collected.append(title)
                continuation = payload.get("continue") or {}
                if not continuation:
                    break
                time.sleep(settings.pool_request_delay_seconds)
            else:
                raise ValueError("Wikipedia link pagination exceeded request limit")
            if batch_start + LINK_QUERY_BATCH < len(page_ids):
                time.sleep(settings.pool_request_delay_seconds)
        return links

    def fetch_articles(self) -> Iterator[ArticleData]:
        pages = self._pages(
            result_limit=settings.pool_max_articles_per_source,
            generator="search",
            gsrsearch=self.seed_title,
            # MediaWiki caps regular-user search results at 20 per request.
            gsrlimit=min(settings.pool_max_articles_per_source, 20),
            gsrnamespace=0,
        )
        articles: list[tuple[int | None, str, ArticleData]] = []
        for page in pages:
            if article := self._to_article(page):
                page_id = page.get("pageid")
                title = str(page.get("title") or "")
                articles.append((int(page_id) if page_id is not None else None, title, article))

        page_ids = [page_id for page_id, _, _ in articles if page_id]
        titles_by_page = self._fetch_links(page_ids)
        created_at = self._fetch_creation_dates(page_ids)
        # Only a link between two articles this source returned can become an
        # InternalLink row: ingestion resolves an outbound URL against the same
        # site, and drops anything it cannot find. Resolving here keeps the
        # payload proportional to what the database will actually store.
        #
        # A link whose target is reached through a redirect is not resolved and
        # is therefore missed. The captured graph undercounts real links; it
        # never invents one.
        url_by_title = {title: article.url for _, title, article in articles}
        for page_id, _, article in articles:
            links = [
                OutboundLink(
                    url=url_by_title[link_title],
                    # MediaWiki reports the link target, not the rendered
                    # anchor: a piped link shows different words on the page.
                    # Stored as the best available value, and never treated as
                    # evidence of what an editor typed.
                    anchor_text=link_title,
                )
                for link_title in titles_by_page.get(page_id or -1, [])
                if link_title in url_by_title and url_by_title[link_title] != article.url
            ]
            update: dict = {"outbound_internal_links": links}
            if (created := created_at.get(page_id or -1)) is not None:
                update["published_at"] = created
            yield article.model_copy(update=update)

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

    def apply_planned_edit(
        self, source, *, original_html: str, updated_html: str
    ) -> PlannedEditOutcome:
        raise NotImplementedError("content-pool sources are read-only")
