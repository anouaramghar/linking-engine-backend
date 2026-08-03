"""WordPress connector — link extraction and publication, no network (MockTransport)."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.connectors.base import TaxonomyData
from app.connectors import wordpress as wordpress_module
from app.connectors.wordpress import WordPressConnector


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
    assert article.outbound_internal_urls == []


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

    assert article.outbound_internal_urls == ["https://en.blog.wordpress.com/second-post/"]


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
    assert article.outbound_internal_urls == []


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
    connector.apply_link(_suggestion())

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
    c.apply_link(_suggestion())
    assert "Tips &amp; &lt;Tricks&gt;" in captured["content"]
    assert 'href="https://example.com/t?a=1&amp;b=2"' in captured["content"]


def test_apply_link_handles_comment_only_content():
    c = make_connector()
    captured = _mock_publish(c, "<!-- wp:latest-posts /-->")

    c.apply_link(_suggestion())

    assert captured["content"].startswith("<!-- wp:latest-posts /-->")
    assert "Read also" in captured["content"]


def test_apply_link_skips_only_on_exact_href():
    # exact target href present -> idempotent no-op
    c = make_connector()
    captured = _mock_publish(c, '<p><a href="https://example.com/t?a=1&b=2">already</a></p>')
    c.apply_link(_suggestion())
    assert captured == {}

    # a *prefix-sharing* href must not suppress the insert (old substring-check bug)
    c = make_connector()
    captured = _mock_publish(c, '<p><a href="https://example.com/t?a=1&b=25">other post</a></p>')
    c.apply_link(_suggestion())
    assert "Read also" in captured["content"]


def _pool_suggestion():
    """A customer post linking out to a read-only content-pool target."""
    return SimpleNamespace(
        source_article=SimpleNamespace(id=1, external_id="10", url="https://example.com/src"),
        target_article=SimpleNamespace(
            title="Photosynthesis", url="https://en.wikipedia.org/wiki/Photosynthesis"
        ),
        anchor_text=None,
    )


def test_apply_link_is_idempotent_for_external_pool_targets():
    """A retry must not append a second block just because the target is off-site.

    The publication worker rolls a claim back to 'approved' when the remote write
    lands but the commit does not, so apply_link runs again on exactly the content
    it just wrote. Matching only same-host hrefs made that check always miss for a
    content-pool target and duplicate the "Read also" block on every attempt.
    """
    published = (
        "<p>body</p>\n<p>Read also: "
        '<a href="https://en.wikipedia.org/wiki/Photosynthesis">Photosynthesis</a></p>'
    )
    c = make_connector()
    captured = _mock_publish(c, published)

    c.apply_link(_pool_suggestion())

    assert captured == {}

    # A different external target still gets inserted.
    c = make_connector()
    captured = _mock_publish(c, published)
    other = _pool_suggestion()
    other.target_article.url = "https://en.wikipedia.org/wiki/Photosystem"
    c.apply_link(other)
    assert captured["content"].count("Read also") == 2
