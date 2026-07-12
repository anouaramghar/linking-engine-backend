"""v3 checks: cleaning and trust scoring are pure functions — no API keys, no DB."""

from app.ml.external.base import ExternalResult
from app.ml.external.cleaning import clean
from app.ml.external.trust_score import trust_score


def r(url, score=0.5):
    return ExternalResult(url=url, title=url, provider_score=score)


def test_clean_drops_fleet_junk_and_duplicate_domains():
    own = {"www.mysite.com"}
    results = [
        r("https://mysite.com/page"),            # own fleet
        r("https://facebook.com/x"),             # blocked
        r("ftp://weird.com/x"),                  # bad scheme
        r("https://good.com/a"),
        r("https://good.com/b"),                 # same domain — capped at one
        r("https://www.other.org/a"),
    ]
    kept = [c.url for c in clean(results, own)]
    assert kept == ["https://good.com/a", "https://www.other.org/a"]


def test_trust_score_orders_sanely():
    authority = trust_score(r("https://en.wikipedia.org/wiki/Topic", 0.8))
    normal = trust_score(r("https://someblog.com/post", 0.8))
    spammy = trust_score(r("http://cheap-deals.xyz/buy?a=1&b=2&c=3&d=4&e=5", 0.8))
    assert authority > normal > spammy
    assert 0.0 <= spammy and authority <= 1.0
