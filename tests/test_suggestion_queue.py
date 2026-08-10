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

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from app.api.pagination import MAX_PAGE_SIZE
from app.api.routes.suggestions import _review_matching_counts
from app.db import SessionLocal
from app.models import Article, Site, Suggestion
from app.schemas.suggestion import BulkReviewFilter, MAX_BULK_REVIEW

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

    body = _get(client, "/api/v1/suggestions", include_total=True)

    assert [item["id"] for item in body["items"]] == [high.id, middle.id, low.id]
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["next_cursor"] is None


def test_site_filter_narrows_the_queue_and_its_total(client, db, site, other_site):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    mine = _suggest(db, site, pair, 0.40)
    _suggest(db, other_site, other_pair, 0.80)
    db.commit()

    body = _get(client, "/api/v1/suggestions", site_id=site.id, include_total=True)

    assert [item["id"] for item in body["items"]] == [mine.id]
    # The total describes the filtered match, not the table.
    assert body["total"] == 1


def test_expired_is_hidden_unless_asked_for(client, db, site):
    pair = _pair(db, site)
    live = _suggest(db, site, pair, 0.60)
    expired = _suggest(db, site, pair, 0.95, status="expired")
    db.commit()

    default = _get(client, "/api/v1/suggestions", site_id=site.id, include_total=True)
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

    seen, cursor = [], None
    while True:
        params = {"site_id": site.id, "limit": 3, "include_total": not seen}
        if cursor is not None:
            params |= {
                "after_score": cursor["score"],
                "after_id": cursor["id"],
            }
        page = _get(client, "/api/v1/suggestions", **params)
        assert page["total"] == (10 if not seen else None)
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)) == 10
    assert set(seen) == expected
    # Ties resolve by descending id, so the whole read is one stable sequence.
    assert seen == sorted(expected, reverse=True)


def test_cursor_does_not_shift_when_rows_above_it_change(client, db, site):
    pair = _pair(db, site)
    suggestions = [_suggest(db, site, pair, score) for score in (0.90, 0.80, 0.70, 0.60)]
    db.commit()

    first = _get(
        client,
        "/api/v1/suggestions",
        site_id=site.id,
        status="pending",
        limit=2,
    )
    assert [item["id"] for item in first["items"]] == [
        suggestions[0].id,
        suggestions[1].id,
    ]

    # A review removes one row above the boundary and a new best row appears.
    # OFFSET would now either skip or repeat an existing row.
    suggestions[0].status = "approved"
    newcomer = _suggest(db, site, pair, 0.99)
    db.commit()

    second = _get(
        client,
        "/api/v1/suggestions",
        site_id=site.id,
        status="pending",
        limit=2,
        after_score=first["next_cursor"]["score"],
        after_id=first["next_cursor"]["id"],
    )
    assert [item["id"] for item in second["items"]] == [
        suggestions[2].id,
        suggestions[3].id,
    ]
    assert newcomer.id not in {item["id"] for item in second["items"]}


def test_cursor_contract_rejects_offsets_and_partial_boundaries(client):
    path = "/api/v1/suggestions"

    assert client.get(path, params={"offset": 0}).status_code == 422
    assert client.get(path, params={"after_score": 0.5}).status_code == 422
    assert client.get(path, params={"after_id": 1}).status_code == 422


def test_total_is_opt_in(client, db, site):
    pair = _pair(db, site)
    _suggest(db, site, pair, 0.50)
    db.commit()

    without_total = _get(client, "/api/v1/suggestions", site_id=site.id)
    with_total = _get(client, "/api/v1/suggestions", site_id=site.id, include_total=True)

    assert without_total["total"] is None
    assert with_total["total"] == 1


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
    # Quarantined rows are returned by the default list, so a chip that cannot
    # report them makes the chips disagree with `total` by exactly this row.
    _suggest(db, site, pair, 0.55, status="failed")
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
        "failed": 1,
        "total": 5,
    }
    # The advertised contract: `total` is what the list returns with no status.
    assert (
        _get(
            client,
            "/api/v1/suggestions",
            site_id=site.id,
            include_total=True,
        )["total"]
        == counts["total"]
    )


def test_counts_respect_the_displayed_percent_window(client, db, site):
    pair = _pair(db, site)
    _suggest(db, site, pair, 0.20)
    _suggest(db, site, pair, 0.80)
    _suggest(db, site, pair, 0.90)
    db.commit()

    counts = _get(client, "/api/v1/suggestions/counts", site_id=site.id, min_percent=80)

    assert (counts["pending"], counts["total"]) == (2, 2)


def test_percent_threshold_partitions_exact_half_boundary(
    client, db, site
):
    pair = _pair(db, site)
    below = _suggest(db, site, pair, 0.7949)
    at = _suggest(db, site, pair, 0.795)
    above = _suggest(db, site, pair, 0.7951)
    db.commit()

    approve_half = _get(
        client,
        "/api/v1/suggestions",
        site_id=site.id,
        status="pending",
        min_percent=80,
        include_total=True,
    )
    reject_half = _get(
        client,
        "/api/v1/suggestions",
        site_id=site.id,
        status="pending",
        max_percent=80,
        include_total=True,
    )
    approve_ids = {item["id"] for item in approve_half["items"]}
    reject_ids = {item["id"] for item in reject_half["items"]}

    assert approve_ids == {at.id, above.id}
    assert reject_ids == {below.id}
    assert approve_ids.isdisjoint(reject_ids)
    assert approve_half["total"] + reject_half["total"] == 3


def test_bulk_rule_approves_displayed_threshold_with_reviewed_ids(
    client, db, site, other_site
):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    below = _suggest(db, site, pair, 0.7949)
    at = _suggest(db, site, pair, 0.795)
    above = _suggest(db, site, pair, 0.80)
    out_of_scope = _suggest(db, other_site, other_pair, 0.95)
    db.commit()

    body = _rule(
        client,
        status="approved",
        site_id=site.id,
        threshold_percent=80,
    ).json()

    assert isinstance(body.pop("undo_operation_id"), str)
    assert body == {
        "reviewed": 2,
        "skipped": 0,
        "reviewed_ids": sorted([at.id, above.id]),
        "status": "approved",
    }
    # 0.795 displays as 80%, so it belongs in the inclusive half.
    assert _status(db, at.id) == "approved"
    assert _status(db, above.id) == "approved"
    assert _status(db, below.id) == "pending"
    assert _status(db, out_of_scope.id) == "pending"


def test_bulk_rule_rejects_strictly_below_the_same_threshold(client, db, site):
    pair = _pair(db, site)
    below = _suggest(db, site, pair, 0.7949)
    at = _suggest(db, site, pair, 0.795)
    db.commit()

    body = _rule(
        client,
        status="rejected",
        site_id=site.id,
        threshold_percent=80,
    ).json()

    assert isinstance(body.pop("undo_operation_id"), str)
    assert body == {
        "reviewed": 1,
        "skipped": 0,
        "reviewed_ids": [below.id],
        "status": "rejected",
    }
    assert _status(db, below.id) == "rejected"
    assert _status(db, at.id) == "pending"


def test_bulk_rule_can_be_scoped_to_one_site(client, db, site, other_site):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    mine = _suggest(db, site, pair, 0.90)
    theirs = _suggest(db, other_site, other_pair, 0.90)
    db.commit()

    body = _rule(
        client,
        status="approved",
        site_id=site.id,
        threshold_percent=80,
    ).json()

    assert body["reviewed"] == 1
    assert _status(db, mine.id) == "approved"
    assert _status(db, theirs.id) == "pending"


def test_bulk_rule_default_match_status_is_pending(client, db, site):
    pair = _pair(db, site)
    applying = _suggest(db, site, pair, 0.99, status="applying")
    applied = _suggest(db, site, pair, 0.99, status="applied")
    expired = _suggest(db, site, pair, 0.99, status="expired")
    pending = _suggest(db, site, pair, 0.99)
    db.commit()

    body = _rule(
        client,
        status="rejected",
        site_id=site.id,
        threshold_percent=100,
    ).json()

    assert isinstance(body.pop("undo_operation_id"), str)
    assert body == {
        "reviewed": 1,
        "skipped": 0,
        "reviewed_ids": [pending.id],
        "status": "rejected",
    }
    assert _status(db, pending.id) == "rejected"
    for untouched, expected in (
        (applying, "applying"),
        (applied, "applied"),
        (expired, "expired"),
    ):
        assert _status(db, untouched.id) == expected


def test_filtered_review_counts_one_snapshot_and_skips_a_concurrent_claim(db, site):
    pair = _pair(db, site)
    claimed = _suggest(db, site, pair, 0.90)
    reviewable = _suggest(db, site, pair, 0.80)
    db.commit()

    claim = SessionLocal()
    observer = SessionLocal()
    backend_pid: Queue[int] = Queue()

    def review_in_other_transaction():
        other = SessionLocal()
        try:
            backend_pid.put(other.scalar(select(text("pg_backend_pid()"))))
            conditions = [
                Suggestion.site_id == site.id,
                Suggestion.status == "pending",
                Suggestion.method == QUEUE_METHOD,
            ]
            result = _review_matching_counts(other, conditions, "rejected")
            other.commit()
            return result
        finally:
            other.close()

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        claim.execute(
            update(Suggestion).where(Suggestion.id == claimed.id).values(status="applied")
        )
        future = executor.submit(review_in_other_transaction)
        pid = backend_pid.get(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            waiting = observer.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).scalar_one()
            observer.rollback()
            if waiting == "Lock":
                break
            time.sleep(0.01)
        else:
            pytest.fail("filtered review did not wait on the publication claim")

        claim.commit()
        matched, reviewed, reviewed_ids = future.result(timeout=2)
    finally:
        claim.rollback()
        claim.close()
        observer.close()
        executor.shutdown(wait=True)

    assert (matched, reviewed, matched - reviewed) == (2, 1, 1)
    assert reviewed_ids == [reviewable.id]
    assert _status(db, claimed.id) == "applied"
    assert _status(db, reviewable.id) == "rejected"


def test_small_filtered_review_can_be_undone_by_returned_ids(client, db, site):
    pair = _pair(db, site)
    pending = _suggest(db, site, pair, 0.90)
    db.commit()

    reviewed = _rule(
        client,
        status="approved",
        site_id=site.id,
        threshold_percent=80,
    ).json()
    assert reviewed["reviewed_ids"] == [pending.id]

    undone = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": reviewed["reviewed_ids"], "status": "pending"},
    ).json()

    assert undone == {"reviewed": [pending.id], "skipped": [], "status": "pending"}
    assert _status(db, pending.id) == "pending"
    assert db.get(Suggestion, pending.id).reviewed_at is None


def test_large_filtered_review_has_exact_idempotent_server_side_undo(client, db, site):
    pair = _pair(db, site)
    suggestions = [
        _suggest(db, site, pair, 0.90) for _ in range(MAX_BULK_REVIEW + 1)
    ]
    db.commit()

    body = _rule(
        client,
        status="approved",
        site_id=site.id,
        threshold_percent=80,
    ).json()

    operation_id = body.pop("undo_operation_id")
    assert isinstance(operation_id, str)
    assert body == {
        "reviewed": MAX_BULK_REVIEW + 1,
        "skipped": 0,
        "reviewed_ids": None,
        "status": "approved",
    }

    # A row that has moved on since the review must not be overwritten by Undo.
    suggestions[0].status = "applied"
    db.commit()
    undone = client.post(
        f"/api/v1/suggestions/bulk-review-operations/{operation_id}/undo"
    )
    assert undone.status_code == 200
    assert undone.json() == {
        "restored": MAX_BULK_REVIEW,
        "skipped": 1,
        "status": "pending",
        "already_undone": False,
    }
    assert _status(db, suggestions[0].id) == "applied"
    assert _status(db, suggestions[-1].id) == "pending"

    repeated = client.post(
        f"/api/v1/suggestions/bulk-review-operations/{operation_id}/undo"
    )
    assert repeated.json() == {
        "restored": MAX_BULK_REVIEW,
        "skipped": 1,
        "status": "pending",
        "already_undone": True,
    }


def test_bulk_rule_matching_nothing_is_not_an_error(client, db, site):
    pair = _pair(db, site)
    _suggest(db, site, pair, 0.10)
    db.commit()

    body = _rule(
        client,
        status="approved",
        site_id=site.id,
        threshold_percent=100,
    ).json()

    assert body == {
        "reviewed": 0,
        "skipped": 0,
        "reviewed_ids": [],
        "undo_operation_id": None,
        "status": "approved",
    }


def test_bulk_rule_cannot_set_a_worker_owned_status(client, site):
    assert _rule(
        client,
        status="applied",
        site_id=site.id,
        threshold_percent=80,
    ).status_code == 422


def test_fleet_wide_bulk_rule_must_be_asked_for_explicitly(client, db, site):
    """A dropped site_id must not silently mean "every site".

    Every other field on the payload narrows the match, so an omitted one widens
    it. This is the one case where widening is unrecoverable, and it is exactly
    what a client bug looks like.
    """
    pair = _pair(db, site)
    untouched = _suggest(db, site, pair, 0.90)
    db.commit()

    resp = _rule(client, status="approved", threshold_percent=80)

    assert resp.status_code == 422
    assert _status(db, untouched.id) == "pending"
    # Validate the contradictory shape without ever issuing an all-sites request
    # against the shared seeded database.
    with pytest.raises(ValidationError):
        BulkReviewFilter(
            status="approved",
            threshold_percent=80,
            site_id=site.id,
            all_sites=True,
        )


def test_paged_queue_and_bulk_rule_agree_on_the_same_filter(client, db, site):
    """What the rule claims to have reviewed is what the list said was there."""
    pair = _pair(db, site)
    for score in (0.30, 0.85, 0.90, 0.95):
        _suggest(db, site, pair, score)
    db.commit()

    listed = _get(
        client,
        "/api/v1/suggestions",
        site_id=site.id,
        status="pending",
        min_percent=85,
        include_total=True,
    )
    reviewed = _rule(
        client,
        status="approved",
        site_id=site.id,
        threshold_percent=85,
    ).json()

    assert listed["total"] == reviewed["reviewed"] == 3
    remaining = db.scalars(
        select(Suggestion.status).where(Suggestion.site_id == site.id)
    ).all()
    assert sorted(remaining) == ["approved", "approved", "approved", "pending"]


def test_pending_publication_lists_only_sites_with_approved_backlogs(
    client, db, site, other_site
):
    pair, other_pair = _pair(db, site), _pair(db, other_site)
    _suggest(db, site, pair, 0.90, status="approved")
    _suggest(db, site, pair, 0.80, status="approved")
    _suggest(db, site, pair, 0.70, status="applied")
    _suggest(db, other_site, other_pair, 0.60, status="pending")
    db.commit()

    pending = client.get("/api/v1/publish/pending").json()
    owned = {
        row["site_id"]: row["awaiting_publication"]
        for row in pending
        if row["site_id"] in {site.id, other_site.id}
    }

    assert owned == {site.id: 2}
    assert client.get(f"/api/v1/publish/{site.id}/status").json()[
        "awaiting_publication"
    ] == owned[site.id]
