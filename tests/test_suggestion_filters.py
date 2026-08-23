"""Queue filters: title search, target origin, and reciprocal-pair exclusion.

Rows are inserted directly rather than generated, so each test states exactly the
corpus its assertion depends on.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import Article, Site, Suggestion

QUEUE = "/api/v1/suggestions"
COUNTS = "/api/v1/suggestions/counts"
BULK_BY_FILTER = "/api/v1/suggestions/bulk-review-by-filter"


def _article(db, site, title, slug=None):
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/{slug or uuid.uuid4().hex[:8]}",
        title=title,
        content_text=title,
    )
    db.add(article)
    db.flush()
    return article


def _suggest(db, site, source, target, score, status="pending"):
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=score,
        rank_score=score,
        status=status,
    )
    db.add(suggestion)
    db.flush()
    return suggestion


@pytest.fixture
def pool_site(db):
    """A content-pool site, cascade-deleted with its articles after the test."""
    site = Site(
        name="pool-site",
        base_url=f"https://pool-{uuid.uuid4().hex[:8]}.example.com",
        platform="pool",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    yield site
    db.delete(site)
    db.commit()


def _ids(response):
    return [item["id"] for item in response.json()["items"]]


def test_search_matches_either_end_of_the_pair(client, db, site):
    """A term is useful whether the editor remembers the source or the target."""
    hooks = _article(db, site, "Understanding React hooks")
    css = _article(db, site, "Modern CSS layout")
    unrelated = _article(db, site, "Deployment checklist")
    to_hooks = _suggest(db, site, css, hooks, 0.9)
    from_hooks = _suggest(db, site, hooks, unrelated, 0.8)
    neither = _suggest(db, site, unrelated, css, 0.7)
    db.commit()

    found = _ids(client.get(QUEUE, params={"site_id": site.id, "q": "hooks"}))
    assert set(found) == {to_hooks.id, from_hooks.id}
    assert neither.id not in found


def test_search_is_case_insensitive_and_matches_mid_word(client, db, site):
    article = _article(db, site, "Understanding React Hooks")
    other = _article(db, site, "Deployment checklist")
    suggestion = _suggest(db, site, other, article, 0.9)
    db.commit()

    for term in ("react", "REACT", "eact Hoo"):
        assert _ids(client.get(QUEUE, params={"site_id": site.id, "q": term})) == [suggestion.id], (
            term
        )


def test_search_treats_like_metacharacters_as_literal_text(client, db, site):
    """`%` and `_` are ordinary characters in a search box, not wildcards."""
    literal = _article(db, site, "Save 50% on hosting")
    snake = _article(db, site, "the_cache_key explained")
    plain = _article(db, site, "Deployment checklist")
    on_literal = _suggest(db, site, plain, literal, 0.9)
    on_snake = _suggest(db, site, plain, snake, 0.8)
    db.commit()

    # A bare '%' would match every row if it reached LIKE unescaped.
    assert _ids(client.get(QUEUE, params={"site_id": site.id, "q": "50%"})) == [on_literal.id]
    # '_' would match any single character, so this would also find "the cache".
    assert _ids(client.get(QUEUE, params={"site_id": site.id, "q": "the_cache"})) == [on_snake.id]


def test_blank_search_does_not_narrow_the_queue(client, db, site):
    """Whitespace must not collapse to '%%' and silently match everything."""
    a = _article(db, site, "First")
    b = _article(db, site, "Second")
    suggestion = _suggest(db, site, a, b, 0.9)
    db.commit()

    for term in ("", "   "):
        assert _ids(client.get(QUEUE, params={"site_id": site.id, "q": term})) == [suggestion.id]


def test_search_bounds_the_term_length(client, site):
    assert client.get(QUEUE, params={"site_id": site.id, "q": "x" * 201}).status_code == 422


def test_target_origin_separates_pool_targets_from_internal_ones(client, db, site, pool_site):
    """The filter reads ownership from the target's site, like the card's badge."""
    source = _article(db, site, "Our article")
    internal_target = _article(db, site, "Our other article")
    pool_target = _article(db, pool_site, "Somebody else's article")
    internal = _suggest(db, site, source, internal_target, 0.9)
    external = _suggest(db, site, source, pool_target, 0.8)
    db.commit()

    assert _ids(client.get(QUEUE, params={"site_id": site.id, "target_origin": "internal"})) == [
        internal.id
    ]
    assert _ids(
        client.get(QUEUE, params={"site_id": site.id, "target_origin": "content_pool"})
    ) == [external.id]

    # The filter agrees with the origin the same row reports when serialized.
    listed = client.get(QUEUE, params={"site_id": site.id}).json()["items"]
    assert {item["id"]: item["target_origin"] for item in listed} == {
        internal.id: "internal",
        external.id: "content_pool",
    }


def test_target_origin_rejects_an_unknown_value(client, site):
    response = client.get(QUEUE, params={"site_id": site.id, "target_origin": "elsewhere"})
    assert response.status_code == 422


def test_exclude_reciprocal_keeps_the_stronger_direction(client, db, site):
    """A reciprocal pair should cost one review decision, not two."""
    a = _article(db, site, "Roundup January")
    b = _article(db, site, "Roundup February")
    stronger = _suggest(db, site, a, b, 0.95)
    weaker = _suggest(db, site, b, a, 0.90)
    db.commit()

    kept = _ids(client.get(QUEUE, params={"site_id": site.id, "exclude_reciprocal": True}))
    assert kept == [stronger.id]
    assert weaker.id not in kept


def test_exclude_reciprocal_breaks_a_score_tie_without_dropping_both(client, db, site):
    """Equal scores must not make each row the other's reason to disappear."""
    a = _article(db, site, "Mirror one")
    b = _article(db, site, "Mirror two")
    first = _suggest(db, site, a, b, 0.9)
    second = _suggest(db, site, b, a, 0.9)
    db.commit()

    kept = _ids(client.get(QUEUE, params={"site_id": site.id, "exclude_reciprocal": True}))
    assert kept == [max(first.id, second.id)]


def test_exclude_reciprocal_leaves_one_way_suggestions_alone(client, db, site):
    a = _article(db, site, "Source")
    b = _article(db, site, "Target")
    one_way = _suggest(db, site, a, b, 0.9)
    db.commit()

    assert _ids(client.get(QUEUE, params={"site_id": site.id, "exclude_reciprocal": True})) == [
        one_way.id
    ]


def test_counts_apply_the_same_filters_as_the_list(client, db, site):
    """The chips label the list, so they have to be counting the same rows."""
    hooks = _article(db, site, "React hooks")
    other = _article(db, site, "Deployment checklist")
    _suggest(db, site, other, hooks, 0.9)
    _suggest(db, site, hooks, other, 0.8, status="approved")
    _suggest(db, site, other, other, 0.7)
    db.commit()

    counts = client.get(COUNTS, params={"site_id": site.id, "q": "hooks"}).json()
    assert counts["pending"] == 1
    assert counts["approved"] == 1
    assert counts["total"] == 2

    listed = client.get(QUEUE, params={"site_id": site.id, "q": "hooks"}).json()["items"]
    assert len(listed) == counts["total"]


def test_bulk_rule_acts_on_exactly_the_filtered_rows(client, db, site):
    """The rule carries the queue's filters, so it cannot reach past them."""
    hooks = _article(db, site, "React hooks")
    other = _article(db, site, "Deployment checklist")
    matching = _suggest(db, site, other, hooks, 0.95)
    also_high_but_unmatched = _suggest(db, site, other, other, 0.96)
    db.commit()

    result = client.post(
        BULK_BY_FILTER,
        json={
            "status": "approved",
            "site_id": site.id,
            "threshold_percent": 90,
            "q": "hooks",
        },
    ).json()

    assert result["reviewed"] == 1
    assert result["reviewed_ids"] == [matching.id]
    db.expire_all()
    assert db.get(Suggestion, matching.id).status == "approved"
    # Scored above the threshold, but outside the search — untouched.
    assert db.get(Suggestion, also_high_but_unmatched.id).status == "pending"


def test_bulk_rule_honours_origin_and_reciprocal_filters(client, db, site, pool_site):
    source = _article(db, site, "Our article")
    pool_target = _article(db, pool_site, "Their article")
    internal_target = _article(db, site, "Our other article")
    external = _suggest(db, site, source, pool_target, 0.95)
    internal = _suggest(db, site, source, internal_target, 0.95)
    db.commit()

    # A reject rule matches strictly below its threshold, so 100 is what takes
    # in every score; 0 would correctly match nothing at all.
    client.post(
        BULK_BY_FILTER,
        json={
            "status": "rejected",
            "site_id": site.id,
            "threshold_percent": 100,
            "target_origin": "content_pool",
        },
    )

    db.expire_all()
    assert db.get(Suggestion, external.id).status == "rejected"
    assert db.get(Suggestion, internal.id).status == "pending"


def test_filters_compose_rather_than_override_each_other(client, db, site, pool_site):
    """Every filter narrows; none of them widens what another one excluded.

    The source title deliberately avoids the search term — search matches either
    end of the pair, so a shared source containing it would match every row here
    and the assertion would be testing nothing.
    """
    source = _article(db, site, "Deployment guide")
    pool_hooks = _article(db, pool_site, "Hooks reference")
    pool_hooks_low = _article(db, pool_site, "Hooks appendix")
    pool_other = _article(db, pool_site, "Unrelated reference")
    internal_hooks = _article(db, site, "More hooks")

    wanted = _suggest(db, site, source, pool_hooks, 0.95)
    _suggest(db, site, source, pool_other, 0.95)  # right origin, wrong term
    _suggest(db, site, source, internal_hooks, 0.95)  # right term, wrong origin
    _suggest(db, site, source, pool_hooks_low, 0.40)  # right both, below threshold
    db.commit()

    found = _ids(
        client.get(
            QUEUE,
            params={
                "site_id": site.id,
                "q": "hooks",
                "target_origin": "content_pool",
                "min_percent": 90,
            },
        )
    )
    assert found == [wanted.id]


def test_filtered_queue_still_pages_by_cursor(client, db, site):
    """Search must not break the continuation the queue pages with."""
    target = _article(db, site, "Shared hooks target")
    sources = [_article(db, site, f"Post {i} about hooks") for i in range(5)]
    expected = [
        _suggest(db, site, source, target, 0.9 - index / 100).id
        for index, source in enumerate(sources)
    ]
    db.commit()

    collected = []
    cursor = {}
    while True:
        page = client.get(
            QUEUE, params={"site_id": site.id, "q": "hooks", "limit": 2, **cursor}
        ).json()
        collected.extend(item["id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        cursor = {
            "after_rank_score": page["next_cursor"]["rank_score"],
            "after_id": page["next_cursor"]["id"],
        }

    assert collected == expected


def test_expired_rows_stay_out_of_a_filtered_queue(client, db, site):
    a = _article(db, site, "Hooks one")
    b = _article(db, site, "Hooks two")
    live = _suggest(db, site, a, b, 0.9)
    _suggest(db, site, b, a, 0.95, status="expired")
    db.commit()

    assert _ids(client.get(QUEUE, params={"site_id": site.id, "q": "hooks"})) == [live.id]


def test_expired_reverse_row_does_not_hide_a_live_one(client, db, site):
    """An expired row is not a review decision, so it cannot displace one."""
    a = _article(db, site, "Live direction")
    b = _article(db, site, "Expired direction")
    live = _suggest(db, site, a, b, 0.90)
    _suggest(db, site, b, a, 0.99, status="expired")
    db.commit()

    assert _ids(client.get(QUEUE, params={"site_id": site.id, "exclude_reciprocal": True})) == [
        live.id
    ]


def test_search_does_not_leak_rows_from_another_site(client, db, site, pool_site):
    """Scoping is still by site_id; a shared title must not cross the boundary."""
    ours = _article(db, site, "Shared title")
    theirs = _article(db, pool_site, "Shared title")
    mine = _suggest(db, site, ours, theirs, 0.9)
    db.commit()

    other_site_rows = db.scalars(select(Suggestion).where(Suggestion.site_id == pool_site.id)).all()
    assert other_site_rows == []
    assert _ids(client.get(QUEUE, params={"site_id": site.id, "q": "Shared"})) == [mine.id]
