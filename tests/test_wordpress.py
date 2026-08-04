"""WordPress connector — link extraction and publication, no network (MockTransport)."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.connectors import wordpress as wordpress_module
from app.connectors.base import TaxonomyData
from app.connectors.wordpress import WordPressConnector

IN_TEXT = 'data-linkmesh="in-text" href="https://example.com/t?a=1&amp;b=2"'
RIVAL_IN_TEXT = 'data-linkmesh="in-text" href="https://example.com/other"'


def make_connector(base_url="https://example.com", wp_username=None, wp_app_password=None):
    site = SimpleNamespace(
        name="wp",
        base_url=base_url,
        wp_username=wp_username,
        wp_app_password=wp_app_password,
    )
    return WordPressConnector(site)


def test_internal_hrefs_resolves_relative_links():
    c = make_connector()
    content = (
        '<p><a href="/rooted/">root-relative</a>'
        '<a href="sibling/">doc-relative</a>'
        '<a href="https://example.com/abs">absolute</a>'
        '<a href="https://other.com/x">external</a>'
        '<a href="mailto:a@b.c">mail</a></p>'
    )
    assert c._internal_hrefs(content, "https://example.com/post/") == [
        "https://example.com/rooted/",
        "https://example.com/post/sibling/",
        "https://example.com/abs",
    ]


def test_internal_hrefs_skips_and_logs_malformed_links(monkeypatch):
    c = make_connector()
    malformed = "https://foo [bad].com"
    content = f'<a href="{malformed}">bad</a><a href="/valid/">valid</a>'
    warnings = []
    monkeypatch.setattr(
        wordpress_module,
        "logger",
        SimpleNamespace(warning=lambda *args: warnings.append(args)),
    )

    assert c._internal_hrefs(content, "https://example.com/post/") == [
        "https://example.com/valid/"
    ]
    assert warnings and "Skipping malformed WordPress href" in warnings[0][0]


def test_resolve_internal_url_follows_redirect_alias():
    c = make_connector()
    canonical = "https://example.com/canonical/"

    def handler(request):
        if request.url.path == "/blog/legacy/":
            return httpx.Response(301, headers={"location": canonical}, request=request)
        return httpx.Response(200, request=request)

    c.client = httpx.Client(
        base_url="https://example.com",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )

    assert c.resolve_internal_url("https://example.com/blog/legacy/") == canonical


def test_to_article_handles_comment_only_rendered_content():
    c = make_connector()
    article = c._to_article(
        {
            "id": 10,
            "link": "https://example.com/dynamic-block",
            "title": {"rendered": "Dynamic block"},
            "content": {"rendered": "<!-- wp:latest-posts /-->"},
            "categories": [],
            "tags": [],
            "date_gmt": None,
        },
        {},
    )

    assert article.content_text == ""
    assert [link.url for link in article.outbound_internal_links] == []


def test_to_article_keeps_category_and_tag_with_same_wordpress_id():
    c = make_connector()
    category = TaxonomyData(kind="category", name="News", external_id="7")
    tag = TaxonomyData(kind="tag", name="Featured", external_id="7")

    article = c._to_article(
        {
            "id": 10,
            "link": "https://example.com/post",
            "title": {"rendered": "Post"},
            "content": {"rendered": "<p>body</p>"},
            "categories": [7],
            "tags": [7],
            "date_gmt": None,
        },
        {("category", 7): category, ("tag", 7): tag},
    )

    assert article.taxonomies == [category, tag]


def test_paginate_continues_when_total_pages_header_is_missing():
    connector = make_connector()
    connector._api_candidates = ["https://example.com/wp-json/wp/v2/"]
    requested_pages = []

    def handler(request):
        page = int(request.url.params["page"])
        requested_pages.append(page)
        if page > 2:
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        return httpx.Response(200, json=[{"id": page}])

    connector.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )

    assert list(connector._paginate("/wp-json/wp/v2/posts")) == [{"id": 1}, {"id": 2}]
    assert requested_pages == [1, 2, 3]


def test_page_url_discovers_wordpress_api_root():
    connector = make_connector("https://example.com/articles/first-post")
    requested = []
    page = '<link rel="https://api.w.org/" href="https://example.com/wp-json/">'

    def handler(request):
        requested.append(str(request.url))
        if request.url == "https://example.com/articles/first-post":
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if request.url.path == "/wp-json/wp/v2/categories":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url)

    connector.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    assert list(connector._paginate("categories")) == []
    assert requested == [
        "https://example.com/articles/first-post",
        "https://example.com/wp-json/wp/v2/categories?per_page=100&page=1",
    ]


def test_page_url_discovers_query_based_wordpress_api_root():
    connector = make_connector("https://example.com/?p=123")
    requested = []
    page = '<link rel="https://api.w.org/" href="https://example.com/?rest_route=/">'

    def handler(request):
        requested.append(request)
        if request.url == "https://example.com/?p=123":
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if request.url.path == "/" and request.url.params["rest_route"] == "/wp/v2/categories":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url)

    connector.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    assert list(connector._paginate("categories")) == []
    assert dict(requested[1].url.params) == {
        "rest_route": "/wp/v2/categories",
        "per_page": "100",
        "page": "1",
    }


def test_root_url_discovers_query_based_wordpress_api_root():
    connector = make_connector()
    requested = []
    page = '<link rel="https://api.w.org/" href="https://example.com/?rest_route=/">'

    def handler(request):
        requested.append(request)
        if request.url.path == "/" and "rest_route" not in request.url.params:
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if request.url.path == "/" and request.url.params["rest_route"] == "/wp/v2/categories":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url)

    connector.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    assert list(connector._paginate("categories")) == []
    assert [request.url.path for request in requested] == ["/", "/"]


def test_query_based_api_root_keeps_front_controller_path():
    connector = make_connector("https://example.com/index.php?p=123")
    candidates = []

    connector._add_api_candidate(
        candidates,
        connector._api_v2_base("https://example.com/index.php?rest_route=/"),
    )

    assert candidates == ["https://example.com/index.php?rest_route=/wp/v2/"]


def test_wordpress_com_page_discovers_public_api_from_feed():
    connector = make_connector("https://wordpress.com/blog")
    requested = []
    page = (
        '<link rel="alternate" type="application/rss+xml" href="https://wordpress.com/blog/feed/">'
    )
    feed = (
        '<?xml version="1.0"?><rss xmlns:wpcom="com-wordpress:feed-additions:1">'
        "<channel><wpcom:site>3584907</wpcom:site></channel></rss>"
    )

    def handler(request):
        requested.append(str(request.url))
        if request.url == "https://wordpress.com/blog":
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if request.url == "https://wordpress.com/blog/feed/":
            return httpx.Response(
                200, content=feed, headers={"content-type": "application/rss+xml"}
            )
        if request.url.path == "/wp/v2/sites/3584907/categories":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url)

    connector.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    assert list(connector._paginate("categories")) == []
    assert requested[-1].startswith(
        "https://public-api.wordpress.com/wp/v2/sites/3584907/categories?"
    )


def test_wordpress_api_reports_html_instead_of_json():
    connector = make_connector()

    def handler(_request):
        return httpx.Response(
            200, text="<html>not an API</html>", headers={"content-type": "text/html"}
        )

    connector.client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="expected JSON"):
        list(connector._paginate("posts"))


def test_wordpress_com_alias_links_use_the_api_canonical_host():
    connector = make_connector("https://wordpress.com/blog")
    article = connector._to_article(
        {
            "id": 10,
            "link": "https://en.blog.wordpress.com/first-post/",
            "title": {"rendered": "Post"},
            "content": {
                "rendered": '<p><a href="https://wordpress.com/blog/second-post/">next</a>'
            },
            "categories": [],
            "tags": [],
            "date_gmt": None,
        },
        {},
    )

    assert [link.url for link in article.outbound_internal_links] == [
        "https://en.blog.wordpress.com/second-post/"
    ]


def test_wordpress_does_not_trust_arbitrary_post_link_hosts():
    connector = make_connector()
    article = connector._to_article(
        {
            "id": 10,
            "link": "https://evil.example/first-post/",
            "title": {"rendered": "Post"},
            "content": {"rendered": '<a href="https://evil.example/second-post/">next</a>'},
            "categories": [],
            "tags": [],
            "date_gmt": None,
        },
        {},
    )

    assert connector._canonical_host is None
    assert [link.url for link in article.outbound_internal_links] == []


def test_wordpress_credentials_are_only_sent_to_configured_origin():
    connector = make_connector(
        "https://example.com", wp_username="editor", wp_app_password="secret"
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "https://other.example/wp-json/wp/v2/categories"},
                request=request,
            )
        return httpx.Response(200, json=[])

    connector.client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    connector._api_candidates = ["https://example.com/wp-json/wp/v2/"]

    assert list(connector._paginate("categories")) == []
    assert requests[0].headers.get("authorization", "").startswith("Basic ")
    assert "authorization" not in requests[1].headers

    requests.clear()
    connector._api_base_url = None
    connector._api_candidates = ["https://other.example/wp-json/wp/v2/"]
    assert list(connector._paginate("categories")) == []
    assert "authorization" not in requests[0].headers


def test_query_based_api_root_is_used_for_publication():
    connector = make_connector("https://example.com/?p=123")
    connector._api_candidates = []
    connector._api_base_url = "https://example.com/?rest_route=%2Fwp%2Fv2%2F"
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": "<p>body</p>"}})
        return httpx.Response(200, json={})

    connector.client = httpx.Client(transport=httpx.MockTransport(handler))
    connector.apply_links([_suggestion()])

    assert [request.url.params["rest_route"] for request in requests] == [
        "/wp/v2/posts/10",
        "/wp/v2/posts/10",
    ]
    assert requests[0].url.params["context"] == "edit"


def _mock_publish(connector, raw_content):
    """Route GET -> post with raw_content; capture the update POST body, if any."""
    captured = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": raw_content}})
        captured["content"] = json.loads(request.content)["content"]
        return httpx.Response(200, json={})

    connector.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    return captured


def _suggestion(anchor_text=None):
    return SimpleNamespace(
        source_article=SimpleNamespace(id=1, external_id="10", url="https://example.com/src"),
        target_article=SimpleNamespace(
            title="Tips & <Tricks>", url="https://example.com/t?a=1&b=2"
        ),
        anchor_text=anchor_text,
    )


def test_apply_link_escapes_html():
    c = make_connector()
    captured = _mock_publish(c, "<p>body</p>")
    c.apply_links([_suggestion()])
    assert "Tips &amp; &lt;Tricks&gt;" in captured["content"]
    assert 'href="https://example.com/t?a=1&amp;b=2"' in captured["content"]


def test_apply_link_handles_comment_only_content():
    c = make_connector()
    captured = _mock_publish(c, "<!-- wp:latest-posts /-->")

    c.apply_links([_suggestion()])

    assert captured["content"].startswith("<!-- wp:latest-posts /-->")
    assert "Read also" in captured["content"]


def test_apply_link_skips_only_on_exact_href():
    # exact target href present -> idempotent no-op
    c = make_connector()
    captured = _mock_publish(c, '<p><a href="https://example.com/t?a=1&b=2">already</a></p>')
    c.apply_links([_suggestion()])
    assert captured == {}

    # a *prefix-sharing* href must not suppress the insert (old substring-check bug)
    c = make_connector()
    captured = _mock_publish(c, '<p><a href="https://example.com/t?a=1&b=25">other post</a></p>')
    c.apply_links([_suggestion()])
    assert "Read also" in captured["content"]


def test_apply_link_inserts_in_text_when_the_anchor_is_locatable():
    """A located anchor is linked in place, and the block is not also appended."""
    c = make_connector()
    captured = _mock_publish(c, "<!-- wp:paragraph --><p>solar&nbsp;panel costs fell</p>")

    c.apply_links([_suggestion(anchor_text="solar panel")])

    # Byte-for-byte outside the two insertion points, `&nbsp;` included.
    assert captured["content"] == (
        f"<!-- wp:paragraph --><p><a {IN_TEXT}>solar&nbsp;panel</a> costs fell</p>"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        # the classic editor stores no <p> at all — wpautop adds it at render time,
        # so an allowlist that insisted on one would refuse every classic post
        (
            "solar panel costs fell last year.",
            f"<a {IN_TEXT}>solar panel</a> costs fell last year.",
        ),
        # list items are prose too
        (
            "<ul><li>solar panel costs fell</li></ul>",
            f"<ul><li><a {IN_TEXT}>solar panel</a> costs fell</li></ul>",
        ),
        # inline formatting around the anchor does not make it unsafe
        (
            "<p><em>solar panel</em> costs fell</p>",
            f"<p><em><a {IN_TEXT}>solar panel</a></em> costs fell</p>",
        ),
    ],
)
def test_apply_link_inserts_in_text_across_prose_contexts(raw, expected):
    c = make_connector()
    captured = _mock_publish(c, raw)

    c.apply_links([_suggestion(anchor_text="solar panel")])

    assert captured["content"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        # already inside a link — nesting <a> is invalid and WordPress rewrites it
        '<p><a href="https://example.com/x">solar panel</a> costs fell</p>',
        # inside a shortcode's attributes, where a tag would break the shortcode
        '<p>[gallery title="solar panel"] costs fell</p>',
        # inside a block delimiter, which is not where the rendered text came from
        '<!-- wp:heading {"content":"solar panel"} --><p>costs fell</p>',
        # broken up by an inline tag — not found, rather than found in the wrong place
        "<p>solar <strong>panel</strong> costs fell</p>",
        # two candidates: no evidence which passage the model meant
        "<p>solar panel costs fell; solar panel demand rose</p>",
        # inside script/style source, which `text_content()` feeds to the model
        # along with the prose — splicing there breaks the page, not a paragraph
        '<p>costs fell</p><script>var t = "solar panel";</script>',
        '<p>costs fell</p><style>.x::after{content:"solar panel"}</style>',
        # only present glued into a longer word — linking half a word is not a link
        "<p>the solar panelling costs fell</p>",
        # a heading is a heading, not a place for a link
        "<h2>solar panel costs</h2><p>fell</p>",
        # and it stays one when a theme wraps its text: the whole open element
        # chain has to be prose, not just the element the text sits directly in
        "<h2><span>solar panel</span> costs</h2><p>fell</p>",
        # a code sample is quoted verbatim; a tag spliced in corrupts what it shows
        "<p>costs fell</p><pre><code>solar panel = 1</code></pre>",
        # a caption describes the figure, it is not body prose
        '<figure><img src="x"><figcaption>solar panel</figcaption></figure>',
        # a quotation is someone else's words, and we do not put links in them
        "<blockquote><p>solar panel costs fell</p></blockquote>",
        # utility containers are not article body copy, even when they contain text
        "<aside><p>solar panel costs fell</p></aside>",
        "<table><tbody><tr><td>solar panel costs fell</td></tr></tbody></table>",
        "<figure>solar panel costs fell</figure>",
    ],
)
def test_apply_link_falls_back_to_the_block_when_in_text_is_unsafe(raw):
    c = make_connector()
    captured = _mock_publish(c, raw)

    c.apply_links([_suggestion(anchor_text="solar panel")])

    assert captured["content"] == raw + (
        '\n<!-- wp:paragraph -->\n<p>Read also: <a href="https://example.com/t?a=1&amp;b=2">solar panel</a></p>\n<!-- /wp:paragraph -->'
    )


def _republish(connector_content, suggestion):
    """Apply `suggestion` to `connector_content`, returning the post afterwards.

    The publication worker serializes writes per source article and re-reads the
    post for each suggestion, so a second `apply_link` genuinely sees the first
    one's markup. These tests replay that sequence — nothing coordinates the two
    calls, which is the point.
    """
    c = make_connector()
    captured = _mock_publish(c, connector_content)
    c.apply_links([suggestion])
    return captured.get("content", connector_content)


def _rival(anchor_text):
    """A second suggestion on the same source article, pointing somewhere else."""
    suggestion = _suggestion(anchor_text=anchor_text)
    suggestion.target_article.url = "https://example.com/other"
    return suggestion


def test_two_suggestions_competing_for_one_anchor():
    """First one wins the anchor in place; the second falls back to the block.

    Nothing arbitrates between them. The existing-link exclusion is what stops
    the second from nesting its <a> inside the first, and the result is still a
    correct link for the loser.
    """
    after_first = _republish(
        "<p>solar panel costs fell</p>", _suggestion(anchor_text="solar panel")
    )
    assert after_first == f"<p><a {IN_TEXT}>solar panel</a> costs fell</p>"

    assert _republish(after_first, _rival("solar panel")) == after_first + (
        '\n<!-- wp:paragraph -->\n<p>Read also: <a href="https://example.com/other">solar panel</a></p>\n<!-- /wp:paragraph -->'
    )


def test_an_anchor_overlapping_one_already_linked_is_not_split():
    """"panel costs" straddles the first link's boundary, so it is no longer text."""
    after_first = _republish(
        "<p>solar panel costs fell</p>", _suggestion(anchor_text="solar panel")
    )

    assert _republish(after_first, _rival("panel costs")) == after_first + (
        '\n<!-- wp:paragraph -->\n<p>Read also: <a href="https://example.com/other">panel costs</a></p>\n<!-- /wp:paragraph -->'
    )


def test_a_plural_elsewhere_does_not_make_the_anchor_ambiguous():
    """Word boundaries earn this placement: "panels" would otherwise be a rival."""
    assert _republish(
        "<p>one panel here, many panels there</p>", _suggestion(anchor_text="panel")
    ) == f"<p>one <a {IN_TEXT}>panel</a> here, many panels there</p>"


#: Long enough that the two anchors are not crowding each other; the gap guard
#: is tested on its own below.
_FILLER = "Installers reported steady interest through the year. " * 8


def test_distinct_anchors_each_get_their_own_in_text_link():
    """Offsets are computed per call, so an earlier splice cannot misplace a later one."""
    after_first = _republish(
        f"<p>solar panel costs fell. {_FILLER}Then demand rose</p>",
        _suggestion(anchor_text="solar panel"),
    )

    assert _republish(after_first, _rival("demand rose")) == (
        f"<p><a {IN_TEXT}>solar panel</a> costs fell. {_FILLER}Then "
        f"<a {RIVAL_IN_TEXT}>demand rose</a></p>"
    )


def test_a_second_in_text_link_too_close_to_the_first_falls_back_to_the_block():
    """Two links a few words apart read as spam however good each one is.

    The same content with the anchors far apart is
    `test_distinct_anchors_each_get_their_own_in_text_link`, so this is the gap
    guard alone, not ambiguity or the per-article limit.
    """
    after_first = _republish(
        "<p>solar panel costs fell as demand rose</p>",
        _suggestion(anchor_text="solar panel"),
    )
    assert after_first.count("data-linkmesh") == 1

    assert _republish(after_first, _rival("demand rose")) == after_first + (
        "\n<!-- wp:paragraph -->\n<p>Read also: "
        '<a href="https://example.com/other">demand rose</a></p>\n<!-- /wp:paragraph -->'
    )


def test_in_text_stops_at_the_per_article_limit(monkeypatch):
    """Past the limit a suggestion still publishes — as the block, not in the prose.

    The limit counts the marks already in the post, so it is a standing property
    of the article rather than of one publication run: a later run cannot keep
    adding links to prose that is already at its cap.
    """
    monkeypatch.setattr(settings, "publish_max_in_text_links_per_article", 1)
    after_first = _republish(
        "<p>solar panel costs fell as demand rose</p>",
        _suggestion(anchor_text="solar panel"),
    )
    assert after_first.count("data-linkmesh") == 1

    # A free, unambiguous anchor — refused only because the article is full.
    assert _republish(after_first, _rival("demand rose")) == after_first + (
        '\n<!-- wp:paragraph -->\n<p>Read also: <a href="https://example.com/other">demand rose</a></p>\n<!-- /wp:paragraph -->'
    )


def _pool_suggestion():
    """A customer post linking out to a read-only content-pool target."""
    return SimpleNamespace(
        source_article=SimpleNamespace(id=1, external_id="10", url="https://example.com/src"),
        target_article=SimpleNamespace(
            title="Photosynthesis", url="https://en.wikipedia.org/wiki/Photosynthesis"
        ),
        anchor_text=None,
    )


def test_publication_is_not_bounded_by_the_crawl_budget(monkeypatch):
    """Publishing a large approved batch is not a crawl overrunning its budget.

    The clock used to start on the connector's first request whatever it was
    for, so a publication run past the crawl deadline failed every remaining
    suggestion with "crawl exceeded ..." — a wrong diagnosis of a real problem,
    which finished only because RQ retried the job with a fresh connector.
    """
    monkeypatch.setattr("app.connectors.http_limits.monotonic", lambda: 1e9)
    c = make_connector()
    captured = _mock_publish(c, "<p>solar panel costs fell</p>")

    c.apply_links([_suggestion(anchor_text="solar panel")])

    assert captured["content"] == f"<p><a {IN_TEXT}>solar panel</a> costs fell</p>"


@pytest.mark.parametrize(
    "crawl",
    [
        pytest.param(lambda c: list(c.fetch_articles()), id="fetch_articles"),
        pytest.param(
            lambda c: c.fetch_article_by_url("https://example.com/a/"),
            id="fetch_article_by_url",
        ),
        pytest.param(lambda c: c.get_site_metadata(), id="get_site_metadata"),
    ],
)
def test_the_crawl_budget_still_bounds_every_crawl_entry_point(crawl, monkeypatch):
    """Taking the deadline off publication must not take it off crawling."""
    monkeypatch.setattr("app.connectors.http_limits.monotonic", lambda: 1e9)
    c = make_connector()
    c._api_candidates = []
    c._api_base_url = "https://example.com/wp-json/wp/v2/"
    c.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    )

    with pytest.raises(ValueError, match="crawl exceeded"):
        crawl(c)


def test_apply_link_is_idempotent_for_external_pool_targets():
    """A retry must not append a second block just because the target is off-site.

    The publication worker rolls a claim back to 'approved' when the remote write
    lands but the commit does not, so apply_link runs again on exactly the content
    it just wrote. Matching only same-host hrefs made that check always miss for a
    content-pool target and duplicate the "Read also" block on every attempt.
    """
    published = (
        "<p>body</p>\n<!-- wp:paragraph -->\n<p>Read also: "
        '<a href="https://en.wikipedia.org/wiki/Photosynthesis">Photosynthesis</a></p>'
        "\n<!-- /wp:paragraph -->"
    )
    c = make_connector()
    captured = _mock_publish(c, published)

    c.apply_links([_pool_suggestion()])

    assert captured == {}

    # A different external target still gets inserted.
    c = make_connector()
    captured = _mock_publish(c, published)
    other = _pool_suggestion()
    other.target_article.url = "https://en.wikipedia.org/wiki/Photosystem"
    c.apply_links([other])
    assert captured["content"].count("Read also") == 2


# -- batched writes (finding 6) --------------------------------------------


def _second_target(anchor_text=None):
    """A second suggestion on the same source, pointing at a different post."""
    suggestion = _suggestion(anchor_text=anchor_text)
    suggestion.target_article.url = "https://example.com/other"
    suggestion.target_article.title = "Other"
    return suggestion


def test_a_batch_reads_and_writes_the_post_once():
    """Three links used to mean three GETs, three POSTs and three WP revisions.

    A revision per link is what the customer sees in their post history, and the
    two extra reads are two more chances for the content to move underneath us.
    """
    c = make_connector()
    c._api_candidates = []  # skip REST-root discovery, which is a GET of its own
    c._api_base_url = "https://example.com/wp-json/wp/v2/"
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "GET":
            raw = f"<p>solar panel costs fell. {_FILLER}Then demand rose. And prices held</p>"
            return httpx.Response(200, json={"content": {"raw": raw}})
        return httpx.Response(200, json={})

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    outcomes = c.apply_links(
        [
            _suggestion(anchor_text="solar panel"),
            _second_target(anchor_text="demand rose"),
            _pool_suggestion(),
        ]
    )

    assert methods == ["GET", "POST"]
    assert outcomes == ["inserted", "inserted", "block"]


def test_a_batch_reports_one_outcome_per_suggestion_in_order():
    """The caller stores these per suggestion, so position has to line up."""
    c = make_connector()
    already = _second_target()
    _mock_publish(c, f'<p>solar panel costs</p><p><a href="{already.target_article.url}">x</a></p>')

    assert c.apply_links(
        [_suggestion(anchor_text="solar panel"), already, _pool_suggestion()]
    ) == ["inserted", "already_present", "block"]


def test_a_batch_that_changes_nothing_does_not_write():
    """Every link already present — a POST here would burn a revision for nothing."""
    c = make_connector()
    captured = _mock_publish(
        c,
        '<p>body <a href="https://example.com/t?a=1&b=2">a</a> '
        '<a href="https://example.com/other">b</a></p>',
    )

    assert c.apply_links([_suggestion(), _second_target()]) == [
        "already_present",
        "already_present",
    ]
    assert captured == {}


def test_dry_run_computes_the_outcomes_without_saving():
    c = make_connector()
    captured = _mock_publish(c, "<p>solar panel costs fell</p>")

    preview = c.preview_links([_suggestion(anchor_text="solar panel")])
    assert preview.original_content == "<p>solar panel costs fell</p>"
    assert f"<a {IN_TEXT}>solar panel</a>" in preview.updated_content
    assert preview.outcomes == ["inserted"]
    assert c.apply_links([_suggestion(anchor_text="solar panel")], dry_run=True) == [
        "inserted"
    ]
    assert captured == {}


def test_a_save_throttled_by_the_host_waits_the_requested_time(monkeypatch):
    """429 means "slow down", and Retry-After says by how much.

    Retrying immediately just collects another 429 and spends the attempt.
    """
    waited = []
    monkeypatch.setattr(wordpress_module, "sleep", waited.append)
    c = make_connector()
    statuses = iter([429, 200])

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": "<p>body</p>"}})
        status = next(statuses)
        headers = {"Retry-After": "12"} if status == 429 else {}
        return httpx.Response(status, json={}, headers=headers)

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    c.apply_links([_suggestion()])

    assert waited == [12.0]


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, 5.0),  # no header — a fixed, short pause
        ("not-a-number", 5.0),
        ("999", 60.0),  # a host asking for 16 minutes does not get to hold the worker
    ],
)
def test_a_throttled_save_without_a_usable_delay_still_waits_sensibly(
    header, expected, monkeypatch
):
    waited = []
    monkeypatch.setattr(wordpress_module, "sleep", waited.append)
    c = make_connector()
    statuses = iter([503, 200])

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": "<p>body</p>"}})
        status = next(statuses)
        return httpx.Response(
            status, json={}, headers={"Retry-After": header} if header else {}
        )

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    c.apply_links([_suggestion()])

    assert waited == [expected]


def test_retry_after_accepts_an_http_date():
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)})

    delay = wordpress_module._retry_after_seconds(response)

    assert 28 <= delay <= 30


def test_a_host_that_keeps_throttling_eventually_fails(monkeypatch):
    monkeypatch.setattr(wordpress_module, "sleep", lambda _seconds: None)
    c = make_connector()
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": "<p>body</p>"}})
        posts.append(request)
        return httpx.Response(429, json={})

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.HTTPStatusError):
        c.apply_links([_suggestion()])

    assert len(posts) == 3  # the first attempt plus two retries


# -- diagnosing a failed write (findings 7 and 4) ---------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_read_the_account_may_not_make_names_the_account(status):
    """`context=edit` is refused before any content, so this is the whole symptom.

    Untranslated it reached the alert as a bare "HTTP 403", which says nothing
    about which of the two things to fix.
    """
    c = make_connector(wp_username="editor-bot", wp_app_password="pw")
    c.client = httpx.Client(
        base_url="https://example.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={})),
    )

    with pytest.raises(ValueError, match=r"editor-bot needs permission to edit posts"):
        c.apply_links([_suggestion()])


def test_a_marker_stripped_by_the_host_is_reported(caplog):
    """The in-text cap counts markers in the stored post — a plugin can delete them.

    Silently, and then the cap is always zero of three and stops existing. The
    link itself published fine, so this warns rather than failing.
    """
    c = make_connector()

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": "<p>solar panel costs</p>"}})
        return httpx.Response(200, json={"content": {"raw": "<p>solar panel costs</p>"}})

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    with caplog.at_level("WARNING"):
        assert c.apply_links([_suggestion(anchor_text="solar panel")]) == ["inserted"]

    assert "0 of 1 in-text markers" in caplog.text


def test_a_marker_that_survives_is_not_reported(caplog):
    c = make_connector()

    def handler(request):
        raw = "<p>solar panel costs</p>"
        if request.method == "POST":
            raw = json.loads(request.content)["content"]
        return httpx.Response(200, json={"content": {"raw": raw}})

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    with caplog.at_level("WARNING"):
        c.apply_links([_suggestion(anchor_text="solar panel")])

    assert "in-text markers" not in caplog.text


def test_a_successful_save_without_returned_raw_content_warns(caplog):
    c = make_connector()

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": "<p>solar panel costs</p>"}})
        return httpx.Response(200, json={})

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    with caplog.at_level("WARNING"):
        c.apply_links([_suggestion(anchor_text="solar panel")])

    assert "in-text marker could not be verified" in caplog.text


def test_a_read_throttled_by_the_host_waits_and_retries(monkeypatch):
    """A batch reads the post before it saves it, and reads get throttled too.

    Only the write honoured Retry-After. An unretried read raises like any other
    failure, which charges every suggestion on the article a publication
    attempt — so three transient 429s quarantine a perfectly healthy row.
    """
    waited = []
    monkeypatch.setattr(wordpress_module, "sleep", waited.append)
    c = make_connector()
    c._api_candidates = []  # REST discovery is its own GET; this test is about the read
    c._api_base_url = "https://example.com/wp-json/wp/v2/"
    reads = iter([429, 200])

    def handler(request):
        if request.method != "GET":
            return httpx.Response(200, json={})
        if next(reads) == 429:
            return httpx.Response(429, json={}, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"content": {"raw": "<p>solar panel costs</p>"}})

    c.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )

    assert c.apply_links([_suggestion(anchor_text="solar panel")]) == ["inserted"]
    assert waited == [7.0]
