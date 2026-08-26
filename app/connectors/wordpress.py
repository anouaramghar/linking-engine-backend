"""WordPress connector — WP REST API, read via public API, write via Application Passwords (A2)."""

import html
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic, sleep
from typing import TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx
from lxml import etree, html as lxml_html
from lxml.etree import ParserError
from lxml.html import HtmlElement

from app.config import settings
from app.connectors.base import (
    ArticleData,
    ContentConnector,
    LinkPreview,
    LinkOutcome,
    OutboundLink,
    PlannedEditOutcome,
    SiteMetadata,
    StalePlanError,
    TaxonomyData,
)
from app.connectors.http_limits import (
    check_crawl_deadline,
    get_limited_http_response,
    request_limited_http_response,
)
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    request_guard,
    validate_url,
)
from app.models.suggestion import Suggestion

T = TypeVar("T")

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
#: Elements a link may be spliced inside. An allowlist, not a list of forbidden
#: regions, because the forbidden list can never close: raw post content is the
#: customer's own markup and the next unlisted element is one custom block away.
#: A position is prose only when *every* element open around it is listed here,
#: so a heading wrapped around a themed <span> is still a heading and a quoted
#: passage stays the words of whoever was quoted. Leaving an element out only
#: costs an in-text link — the suggestion still publishes, as the appended
#: block — while letting one in is how a link ends up in a code sample.
_SPLICE_OK = frozenset(
    # containers a paragraph legitimately sits inside
    "div section article main ul ol dl".split()
    # the text containers themselves
    + "p li dd".split()
    # inline formatting that can wrap part of a sentence
    + "em strong b i u span small sub sup mark".split()
)
#: At least one of these must contain tagged prose. Structural containers are
#: allowed around them, but their own direct text is not body copy. An empty
#: stack remains valid for classic-editor content where WordPress adds <p> only
#: while rendering.
_SPLICE_TEXT = frozenset({"p", "li", "dd"})
#: Elements with no closing tag, which would otherwise stay open for ever and
#: block every later position in the post.
_VOID_ELEMENTS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
#: One tag or comment. Comments carry no name — Gutenberg stores its block
#: delimiters and their attributes in them, and they open nothing.
_TAG_RE = re.compile(r"<!--.*?-->|<(/?)([a-zA-Z][^\s/>]*)[^>]*?(/?)>", re.DOTALL)
#: Shortcodes are excluded separately because they are plain text to the scan
#: above: one sits inside a perfectly ordinary paragraph, and a tag spliced into
#: its attributes breaks it.
_SHORTCODE_RE = re.compile(r"\[[^\]]*\]")
#: Stamped on every link spliced into prose, so the per-article in-text limit can
#: be counted from the post itself. An editor's own links are theirs and are not
#: counted; nothing else in the pipeline reads this, and it stays inert if the
#: limit is later removed. It also makes an automated link identifiable to a
#: human reading the post source, which an unmarked one is not.
_IN_TEXT_MARK = 'data-linkmesh="in-text"'
#: The appended paragraph, serialized as a Gutenberg block. Without the
#: delimiters the block editor cannot recognise it and shows the whole trailing
#: run as one "Classic" block, which an editor then has to convert by hand.
_READ_ALSO_BLOCK = '\n<!-- wp:paragraph -->\n<p>Read also: <a href="{href}">{anchor}</a></p>\n<!-- /wp:paragraph -->'
#: Statuses worth pausing for rather than failing: rate limiting, and the
#: temporary unavailability shared hosting returns under load.
_RETRY_STATUSES = frozenset({429, 503})
_RETRY_ATTEMPTS = 2
_RETRY_FALLBACK_SECONDS = 5.0
_RETRY_MAX_SECONDS = 60.0
#: An anchor is a phrase. A link wrapping a whole section, or an image whose alt
#: text runs long, is stored truncated rather than uncapped.
_MAX_ANCHOR_TEXT_CHARS = 300
logger = logging.getLogger(__name__)


def _suggestion_target_url(suggestion: Suggestion) -> str:
    external_url = getattr(suggestion, "external_url", None)
    if external_url:
        return external_url
    return suggestion.target_article.url


def _suggestion_target_title(suggestion: Suggestion) -> str:
    external_title = getattr(suggestion, "external_title", None)
    if external_title:
        return external_title
    target = suggestion.target_article
    return target.title if target is not None else _suggestion_target_url(suggestion)


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


def _anchor_regex(anchor: str) -> re.Pattern[str]:
    """Match `anchor` in raw post content, absorbing how WordPress stores it.

    An anchor is chosen and verified against the article's *rendered* text with
    whitespace collapsed, but the post is edited through its *raw* content. The
    two differ in two ways common enough to be worth tolerating — the `&nbsp;`
    WordPress writes for spacing, and escaped ampersands. Everything else is
    matched literally: an anchor broken up by inline tags simply is not found,
    which is the right outcome, not a missed case.

    Word-boundary guards are conditional because an anchor may legitimately begin
    or end on punctuation, where a blanket `\\b` asserts the opposite of what is
    wanted. They stop "panel" from linking the middle of "solarpanel", and they
    also *earn* placements: without them "panel" matches "panels" too, and two
    candidates means no placement at all.
    """
    prefix = r"(?<!\w)" if anchor[:1].isalnum() or anchor[:1] == "_" else ""
    suffix = r"(?!\w)" if anchor[-1:].isalnum() or anchor[-1:] == "_" else ""
    return re.compile(
        prefix
        + "".join(
            r"(?:\s|&nbsp;|&#160;)+"
            if token.isspace()
            else r"(?:&|&amp;)"
            if token == "&"
            else re.escape(token)
            for token in re.findall(r"\s+|.", anchor)
        )
        + suffix
    )


def _prose_regions(content: str) -> list[tuple[int, int]]:
    """Spans of raw post content that are ordinary prose, as (start, end) offsets.

    One left-to-right pass tracking which elements are open. Text is prose when
    nothing unlisted is open around it — including when nothing is open at all,
    which is how the classic editor stores a post: bare paragraphs separated by
    blank lines, with the <p> tags added at render time.

    Tags and comments are never part of a region, so block delimiters, tag
    attributes, and <script>/<style> bodies are excluded by construction rather
    than each by its own rule. An unclosed tag blocks the rest of the post
    rather than the reverse.
    """
    regions: list[tuple[int, int]] = []
    stack: list[str] = []
    blocked = 0  # how many open elements are absent from _SPLICE_OK
    cursor = 0
    for match in _TAG_RE.finditer(content):
        prose_open = not stack or any(name in _SPLICE_TEXT for name in stack)
        if not blocked and prose_open and match.start() > cursor:
            regions.append((cursor, match.start()))
        cursor = match.end()
        closing, name, self_closing = match.groups()
        if name is None:  # a comment opens nothing
            continue
        name = name.lower()
        if closing:
            if name in stack:
                # Innermost match, so </div> closes the inner one of a nested pair.
                index = len(stack) - 1 - stack[::-1].index(name)
                blocked -= sum(1 for open_name in stack[index:] if open_name not in _SPLICE_OK)
                del stack[index:]
        elif not self_closing and name not in _VOID_ELEMENTS:
            stack.append(name)
            blocked += name not in _SPLICE_OK
    prose_open = not stack or any(name in _SPLICE_TEXT for name in stack)
    if not blocked and prose_open and cursor < len(content):
        regions.append((cursor, len(content)))
    return regions


def _retry_after_seconds(response: httpx.Response) -> float:
    """How long the server asked us to wait, bounded.

    Both legal HTTP forms are accepted: delta-seconds and an absolute date.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    try:
        return min(max(float(raw), 0.0), _RETRY_MAX_SECONDS)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            return min(max(seconds, 0.0), _RETRY_MAX_SECONDS)
        except (TypeError, ValueError, OverflowError):
            return _RETRY_FALLBACK_SECONDS


def _crowds_existing_link(content: str, span: tuple[int, int]) -> bool:
    """True when another automated link sits too close to `span` to add one more.

    Two links a few words apart read as spam even when each is defensible on its
    own. Measured in raw characters because that is what the splice already
    works in, and the bound only has to be roughly right to stop the obvious
    case. Only our own links are counted: an editor's are theirs to place.
    """
    gap = settings.publish_min_in_text_gap_chars
    if gap <= 0:
        return False
    start, end = span
    return any(
        start - gap < mark.start() < end + gap
        for mark in re.finditer(re.escape(_IN_TEXT_MARK), content)
    )


def _anchor_span(content: str, anchor: str) -> tuple[int, int] | None:
    """The single position `anchor` can be safely linked in place, or None.

    None covers every reason not to link in place, because they are one outcome
    to the caller: the anchor is absent from the raw content, or present only
    outside prose, or present more than once. That last case is a genuine
    unknown — with two candidates there is no evidence which passage the model
    meant, and the appended block links the target correctly either way. Offsets
    rather than a rewritten document: everything outside the two insertion points
    stays byte-for-byte the customer's own markup.
    """
    regions = _prose_regions(content)
    shortcodes = [match.span() for match in _SHORTCODE_RE.finditer(content)]
    spans = [
        match.span()
        for match in _anchor_regex(anchor).finditer(content)
        if any(start <= match.start() and match.end() <= end for start, end in regions)
        and not any(start < match.end() and match.start() < end for start, end in shortcodes)
    ]
    return spans[0] if len(spans) == 1 else None


#: Sentinel marking normal exhaustion of the producer.
_READ_AHEAD_END = object()


def _read_ahead(source: Iterator[T], *, depth: int = 1) -> Iterator[T]:
    """Yield from ``source`` while one worker thread runs it ``depth`` items ahead.

    Ordering, values and exceptions are what a direct iteration would give. A
    failure is handed across the queue and raised only once the consumer has
    reached it, so an error on the page after next cannot surface early and skip
    the items in between.

    Stopping early — a sample limit, or any exception in the consumer — sets the
    stop flag and drains queued values, so a producer parked on a full queue
    wakes, sees the flag and exits. Cleanup never waits for an arbitrary source
    ``next()`` call to finish; once that call returns, the producer sees the flag
    before fetching another item.
    """
    queue: Queue = Queue(maxsize=max(1, depth))
    stop = Event()

    def produce() -> None:
        try:
            source_iterator = iter(source)
            while True:
                if stop.is_set():
                    return
                try:
                    item = next(source_iterator)
                except StopIteration:
                    if not stop.is_set():
                        queue.put(_READ_AHEAD_END)
                    return
                queue.put(item)
                if stop.is_set():
                    return
        except BaseException as error:  # re-raised in the consumer, in order
            if not stop.is_set():
                queue.put(error)

    worker = Thread(target=produce, name="wordpress-read-ahead", daemon=True)
    worker.start()
    try:
        while True:
            item = queue.get()
            if item is _READ_AHEAD_END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
        # Free every queued value so a producer parked on `queue.put` wakes.
        # Do not wait for a producer blocked inside an arbitrary source `next()`;
        # that operation may be a network read with its own much longer timeout.
        while True:
            try:
                queue.get_nowait()
            except Empty:
                break
        worker.join(timeout=0.1)


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

    def _begin_crawl(self) -> None:
        """Start the crawl clock, once, at the top of a crawl.

        Explicit rather than started by whichever request happens to come first,
        because this connector also serves the publication worker and a large
        approved batch is not a crawl overrunning its budget. Sharing one clock
        made a long publication run fail every remaining suggestion with "crawl
        exceeded ...", and finish only because RQ retried the job with a fresh
        connector. Publication is bounded by its own approved batch and by the
        RQ job timeout.
        """
        if self._crawl_started_at is None:
            self._crawl_started_at = monotonic()

    def _get(self, url: str, **kwargs) -> httpx.Response:
        if self._crawl_started_at is not None:
            check_crawl_deadline(self._crawl_started_at)
        return get_limited_http_response(
            self.client,
            url,
            max_bytes=settings.crawl_max_response_bytes,
            crawl_started_at=self._crawl_started_at,
            **kwargs,
        )

    def _pausing_on_throttle(self, send, what: str) -> httpx.Response:
        """Run `send`, honouring the host's own Retry-After before spending an attempt.

        A publication run is the densest traffic LinkMesh ever sends a customer
        site, and shared hosting answers that with 429 or 503. Retrying
        immediately just collects another one.

        Reads need this as much as writes do. A batch reads the post before it
        saves it, and a throttled read raises like any other failure — which
        charges the suggestion a publication attempt and, three transient
        throttles later, quarantines a perfectly healthy row.
        """
        for attempt in range(_RETRY_ATTEMPTS + 1):
            response = send()
            if response.status_code not in _RETRY_STATUSES or attempt == _RETRY_ATTEMPTS:
                return response
            pause = _retry_after_seconds(response)
            logger.warning(
                "WordPress returned HTTP %s %s; waiting %.1fs", response.status_code, what, pause
            )
            sleep(pause)

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
            url if has_rest_route else parts._replace(path=parts.path.rstrip("/") + "/").geturl()
        )
        host = urlparse(candidate).netloc.lower()
        if host not in self._content_hosts and host != "public-api.wordpress.com":
            return
        if candidate not in candidates:
            candidates.append(candidate)

    def _discover_page_links(self) -> tuple[list[str], list[str]]:
        """Find WordPress API/feed links when the submitted URL is a page URL."""
        try:
            response = self._get(self.site.base_url, **self._request_kwargs(self.site.base_url))
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
            response = self._pausing_on_throttle(
                lambda url=url: self._get(url, **self._request_kwargs(url)), f"reading {url}"
            )
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

    def _paginate_pages(self, path: str, params: dict | None = None) -> Iterator[list[dict]]:
        """Yield one REST page of items at a time, newest first."""
        page = 1
        while page <= settings.crawl_max_wordpress_pages:
            resp = self._api_get(path, params={"per_page": 100, "page": page, **(params or {})})
            if resp.status_code == 400:  # WP returns 400 past the last page
                return
            items = self._json(resp)
            if not items:
                return
            yield items
            total_pages = resp.headers.get("X-WP-TotalPages")
            if total_pages is not None:
                declared_total = int(total_pages)
                if declared_total > settings.crawl_max_wordpress_pages:
                    raise ValueError("WordPress pagination exceeded the configured page limit")
                if page >= declared_total:
                    return
            elif page >= settings.crawl_max_wordpress_pages:
                raise ValueError("WordPress pagination exceeded the configured page limit")
            page += 1
        raise ValueError("WordPress pagination exceeded the configured page limit")

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Every item across every page, with the next page fetched while this
        one is being consumed.

        Ingestion writes each article to the database as it arrives, so a plain
        generator left the network idle during the writes and the writes idle
        during the network. One page of read-ahead overlaps them.

        It does not make the crawl any less polite: still one request at a time,
        still at most one page beyond what the caller has reached, and the
        throttle pause moves to the fetching thread rather than disappearing.
        """
        for items in _read_ahead(self._paginate_pages(path, params)):
            yield from items

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
        return [
            link.url for link in self._internal_links_from_tree(_parse_html(content_html), page_url)
        ]

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

    def _internal_links_from_tree(
        self, tree: HtmlElement | None, page_url: str
    ) -> list[OutboundLink]:
        """Internal links with the words they were written on.

        The anchor is read from the element rather than the href attribute
        because this is the only moment it exists: neither article's text can
        reconstruct it afterwards, and it is what an over-optimization report
        needs to see forty pages pointing at one target with identical wording.
        """
        if tree is None:
            return []
        links = []
        for anchor in tree.xpath("//a[@href]"):
            joined = _safe_join(page_url, anchor.get("href"))
            if joined is None:
                continue
            absolute = self._canonicalize_url(joined)
            if self._is_internal_url(absolute):
                text = " ".join(anchor.text_content().split())[:_MAX_ANCHOR_TEXT_CHARS]
                links.append(OutboundLink(url=absolute, anchor_text=text or None))
        return links

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
        post_url = post.get("link")
        if not isinstance(post_url, str):
            raise ValueError("WordPress returned a post without an HTTP(S) link")
        try:
            validate_url(
                post_url,
                allow_private=settings.allow_unsafe_crawl_targets,
                resolve_dns=False,
            )
        except UnsafeURLError as error:
            raise ValueError(f"WordPress returned an unsafe post link: {error}") from error
        post_host = urlparse(post_url).netloc.lower()
        if self._host.lower() == "wordpress.com" and post_host.endswith(".wordpress.com"):
            self._content_hosts.add(post_host)
            self._canonical_host = post_host
        content_html = post["content"]["rendered"]
        content_tree = _parse_html(content_html)
        content_text = _text_content(content_tree)
        if len(content_text) > settings.crawl_max_article_chars:
            raise ValueError(
                f"article content exceeded {settings.crawl_max_article_chars} characters"
            )
        internal_urls = self._internal_links_from_tree(content_tree, post_url)
        if len(internal_urls) > settings.crawl_max_links_per_article:
            raise ValueError(f"article link count exceeded {settings.crawl_max_links_per_article}")
        term_refs = [
            *(("category", term_id) for term_id in post.get("categories", [])),
            *(("tag", term_id) for term_id in post.get("tags", [])),
        ]
        return ArticleData(
            url=post_url,
            external_id=str(post["id"]),
            title=_strip_html(post["title"]["rendered"]),
            content_text=content_text,
            content_html=content_html,
            language=None,  # language filter disabled (A5) — fleet is 100% English
            published_at=_iso(post.get("date_gmt")),
            taxonomies=[taxonomy_map[ref] for ref in term_refs if ref in taxonomy_map],
            outbound_internal_links=internal_urls,
        )

    def fetch_articles(self) -> Iterator[ArticleData]:
        self._begin_crawl()
        taxonomy_map = self._taxonomy_map()
        sample = settings.crawl_sample_articles
        for article_number, post in enumerate(
            self._paginate("posts", {"status": "publish"}),
            start=1,
        ):
            # A sample stops the crawl; the cap refuses it. The two are not the
            # same request: `crawl_max_articles` exists to catch a site far
            # larger than expected, and turning that into a silent truncation
            # would let a partial snapshot deactivate most of a real site.
            if sample and article_number > sample:
                return
            if article_number > settings.crawl_max_articles:
                raise ValueError(f"crawl article count exceeded {settings.crawl_max_articles}")
            yield self._to_article(post, taxonomy_map)

    def fetch_article_by_url(self, url: str) -> ArticleData | None:
        self._begin_crawl()
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        posts = self._json(self._api_get("posts", params={"slug": slug}))
        return self._to_article(posts[0], self._taxonomy_map()) if posts else None

    def get_site_metadata(self) -> SiteMetadata:
        self._begin_crawl()
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

    def _read_post_for_edit(self, source) -> str:
        """The post's raw content, or a diagnosis of why it cannot be read."""
        response = self._api_get(f"posts/{source.external_id}", params={"context": "edit"})
        if response.status_code in {401, 403}:
            # WordPress refuses `context=edit` before it returns any content, so
            # this is what a wrong role or a revoked application password looks
            # like. Untranslated it reaches the alert as a bare HTTP 403.
            raise ValueError(
                f"WordPress rejected an edit-context read of post {source.external_id} "
                f"with HTTP {response.status_code}: the account "
                f"{self.site.wp_username or '(none)'} needs permission to edit posts on "
                f"{self.site.base_url}"
            )
        payload = self._json(response)
        try:
            return payload["content"]["raw"]
        except (KeyError, TypeError) as error:
            raise ValueError(
                f"WordPress returned post {source.external_id} without editable content; "
                "publication needs an account that can edit posts"
            ) from error

    def _insert_link(self, content: str, suggestion: Suggestion) -> tuple[str, LinkOutcome]:
        """One link into `content`: in the prose when that is safe, appended otherwise.

        In-text when the stored placement can still be located in the live post,
        the post is not already at its in-text limit, and the position is not
        crowding a link we placed earlier — appended block otherwise, never
        both. One link per suggestion keeps the caller's idempotency check
        meaningful and the target linked once per article.

        The anchor is re-located here rather than trusted: it was verified
        against a crawl of the rendered page, which is neither this string nor
        this old. The in-text count is read from the content rather than from
        the suggestion table because the limit is a property of the article, and
        because a suggestion that published as a block did not consume a slot in
        the prose.

        ponytail: a block that also stores its text as a delimiter attribute
        (rare, mostly third-party) reads as invalid in the editor after an
        in-text splice; parse block delimiters and restrict to core text blocks
        if that shows up.
        """
        href = html.escape(_suggestion_target_url(suggestion))
        at_limit = content.count(_IN_TEXT_MARK) >= settings.publish_max_in_text_links_per_article
        span = (
            _anchor_span(content, suggestion.anchor_text)
            if suggestion.anchor_text and not at_limit
            else None
        )
        if span is not None and not _crowds_existing_link(content, span):
            start, end = span
            return (
                f'{content[:start]}<a {_IN_TEXT_MARK} href="{href}">'
                f"{content[start:end]}</a>{content[end:]}",
                "inserted",
            )
        anchor = html.escape(suggestion.anchor_text or _suggestion_target_title(suggestion))
        return content + _READ_ALSO_BLOCK.format(href=href, anchor=anchor), "block"

    def _post_content(self, source, content: str) -> httpx.Response:
        """Save `content`, pausing if the host asks us to slow down."""
        url = self._api_url(self._api_base_url or "", f"posts/{source.external_id}")
        return self._pausing_on_throttle(
            lambda: request_limited_http_response(
                self.client,
                "POST",
                url,
                max_bytes=settings.crawl_max_response_bytes,
                json={"content": content},
                **self._request_kwargs(url),
            ),
            f"saving post {source.external_id}",
        )

    def _warn_if_marker_stripped(self, source, sent: str, response: httpx.Response) -> None:
        """The in-text cap is only real if the marker survives the save.

        Modern WordPress keeps `data-*` under KSES, so this is not the default
        case — but a plugin that filters `wp_kses_allowed_html` can drop it, and
        then the count is always zero and the cap silently stops existing. A
        warning rather than a failure: the link itself published correctly.
        """
        expected = sent.count(_IN_TEXT_MARK)
        if not expected:
            return
        try:
            stored = ((response.json() or {}).get("content") or {}).get("raw")
        except ValueError:
            stored = None
        if not isinstance(stored, str):
            logger.warning(
                "WordPress saved post %s but did not return editable content; "
                "the in-text marker could not be verified",
                source.external_id,
            )
            return
        if stored.count(_IN_TEXT_MARK) >= expected:
            return
        logger.warning(
            "WordPress stored %s of %s in-text markers on post %s; the per-article "
            "in-text cap cannot be enforced while the marker is being stripped",
            stored.count(_IN_TEXT_MARK),
            expected,
            source.external_id,
        )

    def preview_links(self, suggestions: list[Suggestion]) -> LinkPreview:
        if not suggestions:
            return LinkPreview("", "", [])
        source = suggestions[0].source_article
        if not source.external_id:
            raise ValueError(f"article {source.id} has no WP post id")
        content = self._read_post_for_edit(source)
        # exact href match, not substring — "/post" must not match href="/post-2".
        # Every href, not just internal ones: the retry-safety this gives the
        # publication worker has to hold for external content-pool targets too.
        existing = set(self._all_hrefs(content, source.url)) if content.strip() else set()

        original = content
        outcomes: list[LinkOutcome] = []
        for suggestion in suggestions:
            target_url = _suggestion_target_url(suggestion)
            if target_url in existing:
                outcomes.append("already_present")
                continue
            content, outcome = self._insert_link(content, suggestion)
            # Two approved suggestions can point at the same target; the second
            # is already present by the time it is reached.
            existing.add(target_url)
            outcomes.append(outcome)
        return LinkPreview(original, content, outcomes)

    def apply_planned_edit(
        self, source, *, original_html: str, updated_html: str
    ) -> PlannedEditOutcome:
        """Send exactly `updated_html`, and only while the post still equals the
        approved `original_html`.

        Nothing here is decided. No suggestion reaches this method, so no anchor
        can be re-located, no fallback can be re-chosen, and no change to a
        target URL, a connector setting, or the placement model after approval
        can alter the bytes that are sent.

        The read immediately before the write is retained because WordPress core
        exposes no compare-and-swap precondition on post updates. It narrows the
        window; it cannot close it. That residual race is documented rather than
        papered over — see docs/design/immutable-publication-plans.md.

        Recognising our own earlier write as `already_applied` is what makes a
        crash between the POST and the database commit recoverable: the retry
        finalizes state instead of appending the same links a second time.
        """
        if not source.external_id:
            raise ValueError(f"article {source.id} has no WP post id")

        live = self._read_post_for_edit(source)
        if live == updated_html:
            return "already_applied"
        if live != original_html:
            raise StalePlanError(
                f"WordPress post {source.external_id} no longer matches the approved "
                "plan; the article changed after it was approved, so nothing was written"
            )

        update = self._post_content(source, updated_html)
        update.raise_for_status()
        self._warn_if_marker_stripped(source, updated_html, update)
        return "written"
