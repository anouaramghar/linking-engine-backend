"""Candidate cleaning: drop own-fleet hosts (external means external), junk domains,
non-http(s) schemes; dedupe by URL and cap one result per domain."""

from urllib.parse import urlparse

from app.ml.external.base import ExternalResult

# ponytail: small static blocklist — move to a DB table if editors need to manage it
BLOCKED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "pinterest.com", "reddit.com", "linkedin.com", "youtube.com", "quora.com",
    "amazon.com", "ebay.com",
}


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def clean(results: list[ExternalResult], own_hosts: set[str]) -> list[ExternalResult]:
    own = {h.lower().removeprefix("www.") for h in own_hosts}
    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    out = []
    for r in results:
        host = _host(r.url)
        if (
            urlparse(r.url).scheme not in ("http", "https")
            or not host
            or host in own
            or host in BLOCKED_DOMAINS
            or r.url in seen_urls
            or host in seen_domains
        ):
            continue
        seen_urls.add(r.url)
        seen_domains.add(host)
        out.append(r)
    return out
