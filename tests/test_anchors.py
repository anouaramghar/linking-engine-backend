"""v4 checks: anchor cleaning and in-text placement are pure functions — no LLM, no DB."""

from app.ml.anchors import _clean_anchor
from app.services.link_placement import inject_inline


def test_clean_anchor():
    assert _clean_anchor('"best hiking trails"', 8) == "best hiking trails"
    assert _clean_anchor("Best hiking trails.", 8) == "Best hiking trails"
    assert _clean_anchor("word", 8) is None  # too short
    assert _clean_anchor("a b c d e f g h i j", 8) is None  # too long


def test_inject_inline_wraps_first_occurrence():
    html = "<p>We love hiking trails in spring.</p><p>More hiking trails here.</p>"
    out = inject_inline(html, "hiking trails", "https://x.com/t")
    assert out.count("<a") == 1
    assert '<a href="https://x.com/t">hiking trails</a> in spring' in out
    assert "More hiking trails here." in out  # second occurrence untouched


def test_inject_inline_is_case_insensitive_and_keeps_original_casing():
    out = inject_inline("<p>Read about Hiking Trails today.</p>", "hiking trails", "https://x.com")
    assert '<a href="https://x.com">Hiking Trails</a>' in out


def test_inject_inline_skips_existing_links_and_headings():
    html = '<h2>hiking trails</h2><p><a href="/old">hiking trails</a> are great.</p>'
    assert inject_inline(html, "hiking trails", "https://x.com") is None


def test_inject_inline_handles_tail_text():
    html = "<p>Intro <em>bold</em> then hiking trails appear.</p>"
    out = inject_inline(html, "hiking trails", "https://x.com")
    assert 'then <a href="https://x.com">hiking trails</a> appear' in out


def test_inject_inline_returns_none_when_absent():
    assert inject_inline("<p>Nothing relevant.</p>", "hiking trails", "https://x.com") is None
