"""The paged cross-site review queue: reading it, counting it, and acting on it by rule.

The dashboard used to pull every suggestion for every site into the browser and
filter there. These endpoints move the paging, the counting, and the bulk rule to
the database, so the tests here care most about what that move can break: page
boundaries under tied scores, counts that must agree with the list they describe,
and a rule that matches rows the client never enumerated.

These are cross-site by nature, and the suite runs against a shared development
database that already holds seeded demo sites. Every row created here therefore
carries `QUEUE_METHOD`, and every request filters on it, so a test can never read
or review a row it did not create. Dropping that filter from a bulk rule is not a
flaky test — it reviews the whole development backlog.
"""

import uuid

import pytest
from sqlalchemy import select

from app.api.pagination import MAX_PAGE_SIZE
from app.models import Article, Site, Suggestion

# The seed data is entirely `baseline_cosine`, so the other method is a free
# namespace for rows this module owns.
QUEUE_METHOD = "gnn_graphsage"


@pytest.fixture
def other_site(db):
    """A second site, so cross-site reads have something to cross."""
    site = Site(
        name="other-site",
        base_url=f"https://other-{uuid.uuid4().hex[:8]}.example.com",
        platform="wordpress",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    yield site
    db.delete(site)
    db.commit()


def _pair(db, site):
    articles = [
        Article(
            site_id=site.id,
            url=f"{site.base_url}/{role}-{uuid.uuid4().hex[:8]}",
            title=role,
            content_text=role,
        )
        for role in ("src", "tgt")
    ]
    db.add_all(articles)
    db.flush()
    return articles


def _suggest(db, site, pair, score, status="pending"):
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=pair[0].id,
        target_article_id=pair[1].id,
        method=QUEUE_METHOD,
        score=score,
        status=status,
    )
    db.add(suggestion)
    db.flush()
    return suggestion


def _get(client, path, **params):
    return client.get(path, params={"method": QUEUE_METHOD, **params}).json()


def _rule(client, **payload):
    return client.post(
        "/api/v1/suggestions/bulk-review-by-filter",
        json={"method": QUEUE_METHOD, **payload},
    )


def _status(db, suggestion_id):
    db.expire_all()
    return db.get(Suggestion, suggestion_id).status


def test_queue_spans_sites_best_score_first(client, db, site, other_site):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    low = _suggest(db, site, pair, 0.10)
    high = _suggest(db, other_site, other_pair, 0.90)
    middle = _suggest(db, site, pair, 0.50)
    db.commit()

    body = _get(client, "/api/v1/suggestions")

    assert [item["id"] for item in body["items"]] == [high.id, middle.id, low.id]
    assert body["total"] == 3
    assert (body["limit"], body["offset"]) == (50, 0)


def test_site_filter_narrows_the_queue_and_its_total(client, db, site, other_site):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    mine = _suggest(db, site, pair, 0.40)
    _suggest(db, other_site, other_pair, 0.80)
    db.commit()

    body = _get(client, "/api/v1/suggestions", site_id=site.id)

    assert [item["id"] for item in body["items"]] == [mine.id]
    # The total describes the filtered match, not the table.
    assert body["total"] == 1


def test_expired_is_hidden_unless_asked_for(client, db, site):
    pair = _pair(db, site)
    live = _suggest(db, site, pair, 0.60)
    expired = _suggest(db, site, pair, 0.95, status="expired")
    db.commit()

    default = _get(client, "/api/v1/suggestions", site_id=site.id)
    assert [item["id"] for item in default["items"]] == [live.id]
    assert default["total"] == 1

    asked = _get(client, "/api/v1/suggestions", site_id=site.id, status="expired")
    assert [item["id"] for item in asked["items"]] == [expired.id]


def test_tied_scores_page_without_repeating_or_losing_rows(client, db, site):
    """Every row exactly once across page boundaries, at a single repeated score.

    Score alone does not order these rows. Without the id tiebreaker PostgreSQL is
    free to return tied rows in a different order per statement, and paging by
    offset would then show some twice and never reach others.
    """
    pair = _pair(db, site)
    expected = {_suggest(db, site, pair, 0.75).id for _ in range(10)}
    db.commit()

    seen = []
    for offset in range(0, 12, 3):
        page = _get(client, "/api/v1/suggestions", site_id=site.id, limit=3, offset=offset)
        assert page["total"] == 10
        seen.extend(item["id"] for item in page["items"])

    assert len(seen) == len(set(seen)) == 10
    assert set(seen) == expected
    # Ties resolve by descending id, so the whole read is one stable sequence.
    assert seen == sorted(expected, reverse=True)


def test_page_size_is_capped(client, site):
    assert client.get(
        "/api/v1/suggestions", params={"limit": MAX_PAGE_SIZE}
    ).status_code == 200
    assert client.get(
        "/api/v1/suggestions", params={"limit": MAX_PAGE_SIZE + 1}
    ).status_code == 422


def test_counts_report_every_status_and_a_total_matching_the_list(
    client, db, site, other_site
):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    _suggest(db, site, pair, 0.10)
    _suggest(db, site, pair, 0.20)
    _suggest(db, site, pair, 0.30, status="approved")
    _suggest(db, site, pair, 0.40, status="applied")
    _suggest(db, site, pair, 0.50, status="expired")
    _suggest(db, other_site, other_pair, 0.60)
    db.commit()

    counts = _get(client, "/api/v1/suggestions/counts", site_id=site.id)

    assert counts == {
        "pending": 2,
        "approved": 1,
        "rejected": 0,
        "applying": 0,
        "applied": 1,
        "expired": 1,
        "total": 4,
    }
    # The advertised contract: `total` is what the list returns with no status.
    assert _get(client, "/api/v1/suggestions", site_id=site.id)["total"] == counts["total"]


def test_counts_respect_the_score_window(client, db, site):
    pair = _pair(db, site)
    _suggest(db, site, pair, 0.20)
    _suggest(db, site, pair, 0.80)
    _suggest(db, site, pair, 0.90)
    db.commit()

    counts = _get(client, "/api/v1/suggestions/counts", site_id=site.id, min_score=0.8)

    assert (counts["pending"], counts["total"]) == (2, 2)


def test_bulk_rule_approves_the_whole_fleet_at_or_above_the_threshold(
    client, db, site, other_site
):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    below = _suggest(db, site, pair, 0.79)
    at = _suggest(db, site, pair, 0.80)
    above = _suggest(db, other_site, other_pair, 0.95)
    db.commit()

    body = _rule(client, status="approved", all_sites=True, min_score=0.8).json()

    assert body == {"reviewed": 2, "skipped": 0, "status": "approved"}
    # Inclusive at the threshold, and it crossed the site boundary.
    assert _status(db, at.id) == "approved"
    assert _status(db, above.id) == "approved"
    assert _status(db, below.id) == "pending"


def test_bulk_rule_rejects_strictly_below_the_same_threshold(client, db, site):
    pair = _pair(db, site)
    below = _suggest(db, site, pair, 0.79)
    at = _suggest(db, site, pair, 0.80)
    db.commit()

    body = _rule(client, status="rejected", site_id=site.id, max_score=0.8).json()

    assert body["reviewed"] == 1
    # Exclusive above, inclusive below: one threshold cannot claim the same row twice.
    assert _status(db, below.id) == "rejected"
    assert _status(db, at.id) == "pending"


def test_bulk_rule_can_be_scoped_to_one_site(client, db, site, other_site):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    mine = _suggest(db, site, pair, 0.90)
    theirs = _suggest(db, other_site, other_pair, 0.90)
    db.commit()

    body = _rule(client, status="approved", site_id=site.id).json()

    assert body["reviewed"] == 1
    assert _status(db, mine.id) == "approved"
    assert _status(db, theirs.id) == "pending"


def test_bulk_rule_never_touches_what_the_publication_worker_owns(client, db, site):
    pair = _pair(db, site)
    applying = _suggest(db, site, pair, 0.99, status="applying")
    applied = _suggest(db, site, pair, 0.99, status="applied")
    expired = _suggest(db, site, pair, 0.99, status="expired")
    pending = _suggest(db, site, pair, 0.99)
    db.commit()

    body = _rule(client, status="rejected", site_id=site.id, max_score=1.0).json()

    assert body["reviewed"] == 1
    assert _status(db, pending.id) == "rejected"
    for untouched, expected in (
        (applying, "applying"),
        (applied, "applied"),
        (expired, "expired"),
    ):
        assert _status(db, untouched.id) == expected


def test_bulk_rule_can_undo_a_previous_decision(client, db, site):
    pair = _pair(db, site)
    approved = _suggest(db, site, pair, 0.90, status="approved")
    db.commit()

    body = _rule(
        client, status="pending", match_status="approved", site_id=site.id
    ).json()

    assert body["reviewed"] == 1
    assert _status(db, approved.id) == "pending"
    assert db.get(Suggestion, approved.id).reviewed_at is None


def test_bulk_rule_matching_nothing_is_not_an_error(client, db, site):
    pair = _pair(db, site)
    _suggest(db, site, pair, 0.10)
    db.commit()

    body = _rule(client, status="approved", site_id=site.id, min_score=0.99).json()

    assert body == {"reviewed": 0, "skipped": 0, "status": "approved"}


def test_bulk_rule_cannot_set_a_worker_owned_status(client, site):
    assert _rule(client, status="applied", site_id=site.id).status_code == 422


def test_fleet_wide_bulk_rule_must_be_asked_for_explicitly(client, db, site):
    """A dropped site_id must not silently mean "every site".

    Every other field on the payload narrows the match, so an omitted one widens
    it. This is the one case where widening is unrecoverable, and it is exactly
    what a client bug looks like.
    """
    pair = _pair(db, site)
    untouched = _suggest(db, site, pair, 0.90)
    db.commit()

    resp = _rule(client, status="approved", min_score=0.8)

    assert resp.status_code == 422
    assert _status(db, untouched.id) == "pending"
    # And the two scopes cannot be given at once.
    assert _rule(
        client, status="approved", site_id=site.id, all_sites=True
    ).status_code == 422


def test_paged_queue_and_bulk_rule_agree_on_the_same_filter(client, db, site):
    """What the rule claims to have reviewed is what the list said was there."""
    pair = _pair(db, site)
    for score in (0.30, 0.85, 0.90, 0.95):
        _suggest(db, site, pair, score)
    db.commit()

    listed = _get(
        client, "/api/v1/suggestions", site_id=site.id, status="pending", min_score=0.85
    )
    reviewed = _rule(client, status="approved", site_id=site.id, min_score=0.85).json()

    assert listed["total"] == reviewed["reviewed"] == 3
    remaining = db.scalars(
        select(Suggestion.status).where(Suggestion.site_id == site.id)
    ).all()
    assert sorted(remaining) == ["approved", "approved", "approved", "pending"]
