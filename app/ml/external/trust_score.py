"""Trust score 0..1 for an external candidate.

ponytail: heuristic blend of provider relevance + domain signals — upgrade path is
real authority data (Moz/Majestic DA, link graphs) if editors reject too much.
"""

from urllib.parse import urlparse

from app.ml.external.base import ExternalResult

AUTHORITY_SUFFIXES = (".gov", ".edu")
AUTHORITY_DOMAINS = {
    "wikipedia.org", "britannica.com", "nih.gov", "who.int", "nature.com",
    "sciencedirect.com", "bbc.com", "reuters.com", "apnews.com", "nytimes.com",
}
SPAMMY_TLDS = (".xyz", ".top", ".click", ".info", ".biz", ".loan")


def trust_score(result: ExternalResult) -> float:
    p = urlparse(result.url)
    host = p.netloc.lower().removeprefix("www.")
    root = ".".join(host.rsplit(".", 2)[-2:])  # sub.domain.tld -> domain.tld

    score = 0.5 * result.provider_score + 0.2  # relevance carries half the weight
    if p.scheme == "https":
        score += 0.1
    if root in AUTHORITY_DOMAINS or host.endswith(AUTHORITY_SUFFIXES):
        score += 0.3
    if host.endswith(SPAMMY_TLDS):
        score -= 0.3
    if len(p.path) > 120 or p.query.count("&") > 3:  # tracking/junk-looking URLs
        score -= 0.1
    return max(0.0, min(1.0, score))
