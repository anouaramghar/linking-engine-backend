"""In-text link placement (v4): wrap the first occurrence of the anchor phrase in the
article HTML with <a>, never inside an existing link, heading, script or style.
Returns None when the phrase doesn't occur — caller falls back to the appended block."""

from lxml import etree
from lxml import html as lxml_html

SKIP_TAGS = {"a", "h1", "h2", "h3", "h4", "h5", "h6", "script", "style"}


def _split(text: str, anchor: str) -> tuple[str, str, str] | None:
    idx = text.lower().find(anchor.lower())
    if idx < 0:
        return None
    return text[:idx], text[idx : idx + len(anchor)], text[idx + len(anchor) :]


def _blocked(el) -> bool:
    return el.tag in SKIP_TAGS or any(a.tag in SKIP_TAGS for a in el.iterancestors())


def inject_inline(content_html: str, anchor: str, url: str) -> str | None:
    if not content_html.strip() or not anchor:
        return None
    root = lxml_html.fragment_fromstring(content_html, create_parent="div")

    for el in root.iter():
        if isinstance(el, lxml_html.HtmlComment) or _blocked(el):
            continue
        # el.text = text inside el, before its first child
        if el.text and (parts := _split(el.text, anchor)):
            before, match, after = parts
            a = lxml_html.Element("a", href=url)
            a.text, a.tail = match, after
            el.text = before
            el.insert(0, a)
            break
        # el.tail = text after el, inside el's parent
        parent = el.getparent()
        if el.tail and parent is not None and not _blocked(parent) and (
            parts := _split(el.tail, anchor)
        ):
            before, match, after = parts
            a = lxml_html.Element("a", href=url)
            a.text, a.tail = match, after
            el.tail = before
            parent.insert(parent.index(el) + 1, a)
            break
    else:
        return None

    return (root.text or "") + "".join(
        etree.tostring(child, encoding="unicode", method="html") for child in root
    )
