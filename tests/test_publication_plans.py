"""Immutable publication plans: the artifact a named operator approves.

A suggestion with status 'approved' has only been *selected*. Nothing about it
says a human has seen the edit it produces. These tests hold the line between
those two facts — the stored artifact, its hash, who bound themselves to it, and
the guarantee that nothing between approval and the WordPress POST can change
what gets written.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import null
from sqlalchemy.exc import IntegrityError, StatementError

from app.config import settings
from app.connectors.base import LinkPreview
from app.api.deps import require_api_key
from app.db import SessionLocal
from app.main import app
from app.models import Article, PublicationPlan, Site, Suggestion
from app.services import publication_plan_service
from app.services.authorization import Principal, ensure_default_tenant
from app.services.publication_plan_service import (
    PlanIntegrityError,
    compute_plan_hash,
    verify_integrity,
)


@pytest.fixture
def articles(db, site):
    src = Article(site_id=site.id, url=f"{site.base_url}/src", title="src", content_text="a")
    tgt = Article(site_id=site.id, url=f"{site.base_url}/tgt", title="tgt", content_text="b")
    db.add_all([src, tgt])
    db.commit()
    return src, tgt  # cascade-deleted with the site


def _suggestion(db, site, src, tgt, status="approved", score=0.9):
    row = Suggestion(
        site_id=site.id,
        source_article_id=src.id,
        target_article_id=tgt.id,
        method="baseline_cosine",
        score=score,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _plan(db, site, source, *, status="prepared", **overrides) -> PublicationPlan:
    fields = {
        "site_id": site.id,
        "source_article_id": source.id,
        "source_url": source.url,
        "status": status,
        "original_html": "<p>Before</p>",
        "updated_html": '<p>Before <a href="https://example.com/tgt">anchor</a></p>',
        "items": [],
        "plan_hash": uuid.uuid4().hex * 2,  # 64 hex characters, unique per row
    }
    fields.update(overrides)
    plan = PublicationPlan(**fields)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


# -- the persisted artifact ------------------------------------------------


def test_a_plan_stores_the_exact_edit_it_describes(db, site, articles):
    plan = _plan(db, site, articles[0])

    assert plan.status == "prepared"
    assert plan.original_html == "<p>Before</p>"
    assert plan.approved_hash is None
    assert plan.approved_by is None
    assert plan.approved_at is None
    assert plan.applied_at is None
    assert plan.created_at is not None


@pytest.mark.parametrize(
    "field",
    ["source_url", "original_html", "updated_html", "items", "plan_hash"],
)
def test_the_artifact_fields_a_hash_covers_cannot_be_null(db, site, articles, field):
    """Every hashed field is part of what the operator agreed to.

    A nullable one would let a plan exist whose canonical form cannot be built,
    which is a plan nobody can verify at publication time. `items` is passed as a
    SQL NULL rather than Python None, because a JSONB column stores None as the
    JSON value `null` and would satisfy NOT NULL without holding a list.
    """
    empty = null() if field == "items" else None
    with pytest.raises(IntegrityError):
        _plan(db, site, articles[0], **{field: empty})
    db.rollback()


def test_a_plan_status_outside_the_lifecycle_is_refused(db, site, articles):
    """Guarded in the type, as every other status column in this schema is.

    `native_enum=False` without a CHECK constraint is the codebase's existing
    choice — it keeps status changes out of migrations — so the refusal happens
    when the value is bound, not in PostgreSQL.
    """
    with pytest.raises((IntegrityError, StatementError, LookupError)):
        _plan(db, site, articles[0], status="published")
    db.rollback()


# -- one live snapshot per source article ----------------------------------


@pytest.mark.parametrize("second_status", ["prepared", "approved"])
def test_two_active_plans_for_one_source_article_cannot_coexist(db, site, articles, second_status):
    """Both would look publishable while only one can be correct.

    Publication replaces the whole post, so two snapshots of the same article are
    two different futures for one WordPress revision.
    """
    _plan(db, site, articles[0], status="approved")
    with pytest.raises(IntegrityError):
        _plan(db, site, articles[0], status=second_status)
    db.rollback()


@pytest.mark.parametrize("terminal", ["applied", "stale", "superseded", "failed"])
def test_a_terminal_plan_does_not_block_the_next_one(db, site, articles, terminal):
    """History has to accumulate: a stale plan is exactly what a re-preparation
    follows, and the audit trail of one article is unbounded by design."""
    _plan(db, site, articles[0], status=terminal)
    replacement = _plan(db, site, articles[0], status="prepared")

    assert replacement.id is not None


def test_two_source_articles_each_get_their_own_active_plan(db, site, articles):
    first = _plan(db, site, articles[0], status="approved")
    second = _plan(db, site, articles[1], status="approved")

    assert first.id != second.id


# -- the suggestion link ---------------------------------------------------


def test_a_suggestion_starts_with_no_plan(db, site, articles):
    """Existing selected rows migrate this way, and preparation does not change it.

    The link is set at final approval only. Until then a selected suggestion
    belongs to nothing, which is what stops an old backlog from being
    grandfathered into an approval nobody gave.
    """
    assert _suggestion(db, site, *articles).publication_plan_id is None


def test_deleting_a_plan_keeps_its_suggestions(db, site, articles):
    """ON DELETE SET NULL, not CASCADE.

    A plan is a rendering of editorial decisions. Deleting the rendering must
    never delete the decisions — that would silently discard review work.
    """
    plan = _plan(db, site, articles[0])
    suggestion = _suggestion(db, site, *articles)
    suggestion.publication_plan_id = plan.id
    db.commit()

    db.delete(plan)
    db.commit()

    db.expire_all()
    survivor = db.get(Suggestion, suggestion.id)
    assert survivor is not None
    assert survivor.publication_plan_id is None


# -- the hash ---------------------------------------------------------------
#
# The hash is the whole approval. If two different edits can produce the same
# digest, an operator's signature covers an edit they never read.


def _artifact(db, site, source, **overrides) -> PublicationPlan:
    """An unsaved plan, so a hash can be computed over it before it is stored."""
    fields = {
        "site_id": site.id,
        "source_article_id": source.id,
        "source_url": source.url,
        "original_html": "<p>Before</p>",
        "updated_html": '<p>Before <a href="https://example.com/tgt">anchor</a></p>',
        "items": [
            {
                "position": 0,
                "suggestion_id": 101,
                "target_url": "https://example.com/tgt",
                "anchor_text": "anchor",
                "outcome": "inserted",
            }
        ],
    }
    fields.update(overrides)
    return PublicationPlan(**fields)


def test_the_same_artifact_always_hashes_to_the_same_value(db, site, articles):
    """Recomputed at approval and again at publication, on different processes,
    possibly on different releases. It has to be a function of the values alone."""
    first = compute_plan_hash(_artifact(db, site, articles[0]))
    second = compute_plan_hash(_artifact(db, site, articles[0]))

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"source_url": "https://example.com/other"},
        {"original_html": "<p>Before </p>"},
        {"updated_html": '<p>Before <a href="https://evil.example/">anchor</a></p>'},
        {"items": []},
    ],
    ids=["source_url", "original_html", "updated_html", "items"],
)
def test_changing_any_covered_field_changes_the_hash(db, site, articles, change):
    baseline = compute_plan_hash(_artifact(db, site, articles[0]))

    assert compute_plan_hash(_artifact(db, site, articles[0], **change)) != baseline


def test_reordering_the_links_changes_the_hash(db, site, articles):
    """Order is the anchor arbitration, not presentation.

    Two links swapped is a different edit: whichever renders first takes the
    contested phrase and the other falls back to the appended block.
    """
    items = [
        {
            "position": position,
            "suggestion_id": suggestion_id,
            "target_url": f"https://example.com/{suggestion_id}",
            "anchor_text": "anchor",
            "outcome": "inserted",
        }
        for position, suggestion_id in enumerate((101, 102))
    ]
    reversed_items = [
        {**item, "position": position} for position, item in enumerate(reversed(items))
    ]

    assert compute_plan_hash(_artifact(db, site, articles[0], items=items)) != compute_plan_hash(
        _artifact(db, site, articles[0], items=reversed_items)
    )


def test_non_ascii_content_is_hashed_stably(db, site, articles):
    """Customer posts are not ASCII. An encoding that escaped them would make the
    digest depend on the json library rather than on the bytes."""
    plan = _artifact(db, site, articles[0], updated_html="<p>Coût d'électricité — 日本語</p>")

    assert compute_plan_hash(plan) == compute_plan_hash(plan)
    assert compute_plan_hash(plan) != compute_plan_hash(
        _artifact(db, site, articles[0], updated_html="<p>Cout d'electricite - 日本語</p>")
    )


def _hashed(db, site, source, **overrides) -> PublicationPlan:
    """A persisted plan whose stored hash actually covers its own artifact."""
    plan = _artifact(db, site, source, status="prepared", **overrides)
    plan.plan_hash = compute_plan_hash(plan)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def test_verify_integrity_accepts_an_untouched_plan(db, site, articles):
    verify_integrity(_hashed(db, site, articles[0]))  # does not raise


def test_verify_integrity_catches_an_artifact_edited_under_its_own_hash(db, site, articles):
    plan = _hashed(db, site, articles[0])

    plan.updated_html = '<p>Before <a href="https://evil.example/">anchor</a></p>'

    with pytest.raises(PlanIntegrityError, match="does not match its stored hash"):
        verify_integrity(plan)


def test_verify_integrity_catches_a_consistently_rewritten_artifact(db, site, articles):
    """The attacker who also updates `plan_hash`.

    `plan_hash` is written by whoever writes the row. `approved_hash` is what a
    human agreed to, so it is the value that has to still match.
    """
    plan = _hashed(db, site, articles[0])
    plan.approved_hash = plan.plan_hash
    plan.status = "approved"
    db.commit()

    plan.updated_html = '<p>Before <a href="https://evil.example/">anchor</a></p>'
    plan.plan_hash = compute_plan_hash(plan)

    with pytest.raises(PlanIntegrityError, match="no longer matches the artifact"):
        verify_integrity(plan)


# -- preparation ------------------------------------------------------------


@pytest.fixture
def other_site(db):
    """A second owned site, for the cross-site approval check."""
    tenant = ensure_default_tenant(db)
    row = Site(
        name="other-site",
        base_url=f"https://other-{uuid.uuid4().hex[:8]}.example.com",
        platform="wordpress",
        tenant_id=tenant.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    yield row
    db.delete(row)
    db.commit()


def _status(db, suggestion_id):
    db.expire_all()
    return db.get(Suggestion, suggestion_id).status


def _stub_preview(monkeypatch, updated=None, outcomes=None, fail=None):
    """A connector that can render an article and cannot write one.

    `apply_planned_edit` is deliberately absent: preparation reaching for it
    would be the exact bug this whole design removes, and an AttributeError says
    so louder than an assertion at the end of a test.
    """
    seen = []

    def preview_links(suggestions):
        seen.append([row.id for row in suggestions])
        if fail is not None:
            raise fail
        return LinkPreview(
            original_content="<p>Before</p>",
            updated_content=updated or '<p>Before <a href="https://example.com/tgt">anchor</a></p>',
            outcomes=outcomes or ["inserted"] * len(suggestions),
        )

    monkeypatch.setattr(
        "app.services.publication_plan_service.get_connector",
        lambda site: SimpleNamespace(preview_links=preview_links),
    )
    return seen


def _approve(client, site, plans, expect=200):
    response = client.post(
        f"/api/v1/publish/{site.id}/plans/approve",
        json={"plans": [{"id": plan["id"], "plan_hash": plan["plan_hash"]} for plan in plans]},
    )
    assert response.status_code == expect, response.text
    return response


def _prepared(client, db, site, articles, monkeypatch, **kwargs):
    """One selected suggestion, prepared into a plan the operator can approve."""
    suggestion = _suggestion(db, site, *articles)
    _stub_preview(monkeypatch, **kwargs)
    plan = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]
    return suggestion, plan


def test_preparation_stores_an_approvable_plan_and_writes_no_wordpress_content(
    client, db, site, articles, monkeypatch
):
    suggestion = _suggestion(db, site, *articles)
    seen = _stub_preview(monkeypatch)

    body = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()

    assert seen == [[suggestion.id]]
    assert body["selected_suggestions"] == 1
    assert body["has_more"] is False
    assert body["errors"] == []
    (plan,) = body["plans"]
    assert plan["status"] == "prepared"
    assert len(plan["plan_hash"]) == 64
    assert plan["original_html"] == "<p>Before</p>"
    assert plan["links"] == [
        {
            "position": 0,
            "suggestion_id": suggestion.id,
            "target_url": articles[1].url,
            "anchor_text": None,
            "outcome": "inserted",
        }
    ]
    # Nothing about the review decision moved, and nothing is bound yet.
    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert (stored.status, stored.publication_plan_id) == ("approved", None)


def test_preparing_an_unchanged_article_twice_returns_the_same_plan(
    client, db, site, articles, monkeypatch
):
    """Re-previewing must not churn the row an operator may be looking at.

    A new id and a new hash on every refresh would invalidate an approval the
    moment anyone reloaded the page.
    """
    _suggestion(db, site, *articles)
    _stub_preview(monkeypatch)

    first = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]
    second = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]

    assert (first["id"], first["plan_hash"]) == (second["id"], second["plan_hash"])


def test_a_changed_article_supersedes_the_previous_prepared_plan(
    client, db, site, articles, monkeypatch
):
    _suggestion(db, site, *articles)
    _stub_preview(monkeypatch)
    first = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]

    _stub_preview(monkeypatch, updated="<p>Before, rewritten</p>")
    second = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]

    assert second["id"] != first["id"]
    db.expire_all()
    assert db.get(PublicationPlan, first["id"]).status == "superseded"
    assert db.get(PublicationPlan, second["id"]).status == "prepared"


def test_an_approved_row_is_not_offered_for_preparation_again(
    client, db, site, articles, monkeypatch
):
    """A suggestion bound to an approved plan is spoken for.

    Re-selecting it into a second artifact is how one editorial decision would
    become two links on the same article.
    """
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])

    _stub_preview(monkeypatch, updated="<p>Before, rewritten</p>")
    body = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()

    assert body["plans"] == []
    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "approved"


def test_an_approved_plan_is_never_superseded_by_a_new_preparation(
    client, db, site, articles, monkeypatch
):
    """A human is already bound to that artifact. Replacing it under them would
    make the approval describe an edit that no longer exists — so a suggestion
    selected afterwards waits rather than rewriting the approved plan.
    """
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])
    late = Article(site_id=site.id, url=f"{site.base_url}/late", title="late", content_text="c")
    db.add(late)
    db.commit()
    latecomer = _suggestion(db, site, articles[0], late)

    _stub_preview(monkeypatch, updated="<p>Before, rewritten</p>")
    body = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()

    assert body["plans"] == []
    assert "already covers this article" in body["errors"][0]["message"]
    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "approved"
    assert db.get(Suggestion, latecomer.id).publication_plan_id is None


def test_approval_wins_when_it_races_with_a_slow_repreparation(
    client, db, site, articles, monkeypatch
):
    """A preview may be slow, but its stale read cannot erase a later approval."""
    suggestion, plan = _prepared(client, db, site, articles, monkeypatch)
    preview_started = Event()
    release_preview = Event()

    def preview_links(_rows):
        preview_started.set()
        assert release_preview.wait(timeout=5)
        return LinkPreview("<p>Before</p>", "<p>Changed during preview</p>", ["inserted"])

    monkeypatch.setattr(
        publication_plan_service,
        "get_connector",
        lambda _site: SimpleNamespace(preview_links=preview_links),
    )

    def reprepare():
        session = SessionLocal()
        try:
            current_site = session.get(Site, site.id)
            return publication_plan_service.prepare_site(session, current_site, max_articles=10)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reprepare)
        assert preview_started.wait(timeout=5)
        approval_db = SessionLocal()
        try:
            publication_plan_service.approve_plans(
                approval_db,
                site.id,
                [(plan["id"], plan["plan_hash"])],
                approved_by="telegram:4242",
            )
        finally:
            approval_db.close()
        release_preview.set()
        preparation = future.result(timeout=5)

    assert preparation.plans == []
    assert "already covers this article" in preparation.errors[0].message
    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "approved"
    assert db.get(Suggestion, suggestion.id).publication_plan_id == plan["id"]


def test_an_unreachable_source_produces_an_error_and_no_plan(
    client, db, site, articles, monkeypatch
):
    """One dead post must not lose the preparation of the others, and must not
    silently ride along in someone else's approval either."""
    _suggestion(db, site, *articles)
    _stub_preview(monkeypatch, fail=RuntimeError("post is gone"))

    body = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()

    assert body["plans"] == []
    assert body["errors"][0]["message"] == "post is gone"
    assert body["errors"][0]["source_article_id"] == articles[0].id


def test_preparation_is_bounded_and_says_so(client, db, site, articles, monkeypatch):
    """`has_more` invites the next batch. It never means the unshown articles
    will be swept into this approval."""
    target = articles[1]
    for index in range(3):
        source = Article(
            site_id=site.id,
            url=f"{site.base_url}/s{index}",
            title=f"s{index}",
            content_text="a",
        )
        db.add(source)
        db.commit()
        _suggestion(db, site, source, target)
    seen = _stub_preview(monkeypatch)

    body = client.post(
        f"/api/v1/publish/{site.id}/plans/prepare", params={"max_articles": 2}
    ).json()

    assert len(seen) == 2
    assert len(body["plans"]) == 2
    assert body["has_more"] is True


def test_a_block_fallback_is_frozen_exactly_as_it_was_shown(
    client, db, site, articles, monkeypatch
):
    """The deterministic appended block is a real, approvable edit.

    It used to be a placeholder that a later placement call could silently
    upgrade to an in-text link, so the operator approved one thing and the site
    received another.
    """
    _suggestion(db, site, *articles)
    block_html = (
        "<p>Before</p>\n<!-- wp:paragraph -->\n<p>Read also: "
        '<a href="https://example.com/tgt">tgt</a></p>\n<!-- /wp:paragraph -->'
    )
    _stub_preview(monkeypatch, updated=block_html, outcomes=["block"])

    plan = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]

    assert plan["updated_html"] == block_html
    assert plan["links"][0]["outcome"] == "block"


def test_preparation_refuses_a_content_pool_source(client, db, site):
    site.platform = "pool"
    db.commit()

    assert client.post(f"/api/v1/publish/{site.id}/plans/prepare").status_code == 409


def test_preparation_refuses_a_site_with_no_wordpress_account(
    client, db, site, articles, monkeypatch
):
    """Refused once, before any live request, rather than per source article.

    Preparation reads every source post with `context=edit`, which WordPress
    answers with a 401 for an anonymous caller. Without this gate a site with no
    account spends one request per article to collect the same error N times and
    then shows the operator an empty review.
    """
    _suggestion(db, site, *articles)
    seen = _stub_preview(monkeypatch)
    site.wp_username = None
    site.wp_app_password = None
    db.commit()

    response = client.post(f"/api/v1/publish/{site.id}/plans/prepare")

    assert response.status_code == 409
    assert "application password" in response.json()["detail"]
    assert seen == []  # nothing was rendered, so nothing was read from the site


# -- approval ---------------------------------------------------------------


def test_approval_records_the_person_and_binds_them_to_the_hash(
    client, db, site, articles, monkeypatch
):
    suggestion, plan = _prepared(client, db, site, articles, monkeypatch)

    body = _approve(client, site, [plan]).json()

    assert body["approved"] == [plan["id"]]
    db.expire_all()
    stored = db.get(PublicationPlan, plan["id"])
    assert stored.status == "approved"
    assert stored.approved_hash == plan["plan_hash"]
    assert stored.approved_by == body["approved_by"]
    assert stored.approved_at is not None
    assert db.get(Suggestion, suggestion.id).publication_plan_id == plan["id"]


def test_a_shared_service_key_cannot_approve(client, db, site, articles, monkeypatch):
    """ "approved_by: linkmesh-api" is not an audit trail.

    The dashboard proxy attaches the shared key, so this is the path a real
    deployment takes; approval needs a person behind it.
    """
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    monkeypatch.setattr(settings, "api_key", "sekret")  # defeat the dev fallback

    _approve(client, site, [plan], expect=401)

    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "prepared"


def test_a_stale_hash_approves_nothing(client, db, site, articles, monkeypatch):
    """The operator's screen described a different edit from the one on the
    server, so the only honest answer is "reload and look again"."""
    _, plan = _prepared(client, db, site, articles, monkeypatch)

    _approve(client, site, [{**plan, "plan_hash": "0" * 64}], expect=409)

    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "prepared"


def test_an_artifact_mutated_since_preparation_cannot_be_approved(
    client, db, site, articles, monkeypatch
):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    db.expire_all()
    db.get(PublicationPlan, plan["id"]).updated_html = "<p>something else entirely</p>"
    db.commit()

    response = _approve(client, site, [plan], expect=409)

    assert "does not match its stored hash" in response.json()["detail"]


def test_a_plan_belonging_to_another_site_cannot_be_approved(
    client, db, site, other_site, articles, monkeypatch
):
    _, plan = _prepared(client, db, site, articles, monkeypatch)

    response = client.post(
        f"/api/v1/publish/{other_site.id}/plans/approve",
        json={"plans": [{"id": plan["id"], "plan_hash": plan["plan_hash"]}]},
    )

    assert response.status_code == 409
    assert "belongs to another site" in response.json()["detail"]


def test_a_superseded_plan_cannot_be_approved(client, db, site, articles, monkeypatch):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _stub_preview(monkeypatch, updated="<p>Before, rewritten</p>")
    client.post(f"/api/v1/publish/{site.id}/plans/prepare")

    response = _approve(client, site, [plan], expect=409)

    assert "is superseded, not prepared" in response.json()["detail"]


def test_approving_the_same_plan_twice_is_refused(client, db, site, articles, monkeypatch):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])

    response = _approve(client, site, [plan], expect=409)

    assert "is approved, not prepared" in response.json()["detail"]


def test_a_suggestion_reviewed_since_preparation_blocks_the_approval(
    client, db, site, articles, monkeypatch
):
    """The artifact still hashes correctly; the intent behind it does not hold."""
    suggestion, plan = _prepared(client, db, site, articles, monkeypatch)
    reviewed = client.put(f"/api/v1/suggestions/{suggestion.id}", json={"status": "rejected"})
    assert reviewed.status_code == 200

    response = _approve(client, site, [plan], expect=409)

    assert f"suggestion {suggestion.id}" in response.json()["detail"]
    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "prepared"


def test_one_bad_plan_in_a_batch_approves_none_of_them(client, db, site, articles, monkeypatch):
    """Approving "the ones that still match" approves a set nobody read."""
    target = articles[1]
    for index in range(2):
        source = Article(
            site_id=site.id, url=f"{site.base_url}/s{index}", title="s", content_text="a"
        )
        db.add(source)
        db.commit()
        _suggestion(db, site, source, target)
    _stub_preview(monkeypatch)
    plans = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"]
    assert len(plans) == 2

    _approve(client, site, [plans[0], {**plans[1], "plan_hash": "0" * 64}], expect=409)

    db.expire_all()
    assert [db.get(PublicationPlan, plan["id"]).status for plan in plans] == [
        "prepared",
        "prepared",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"plans": []},
        {"plans": [{"id": 1, "plan_hash": "abc"}]},
        {"plans": [{"id": 1, "plan_hash": "a" * 64}, {"id": 1, "plan_hash": "b" * 64}]},
    ],
    ids=["empty", "short_hash", "duplicate_id"],
)
def test_a_malformed_approval_is_refused_at_the_schema(client, site, payload):
    assert client.post(f"/api/v1/publish/{site.id}/plans/approve", json=payload).status_code == 422


# -- a suggestion inside an approved plan is spoken for ---------------------


def test_a_suggestion_in_an_approved_plan_cannot_be_reviewed(
    client, db, site, articles, monkeypatch
):
    """Undo would leave the approved artifact describing a link nobody wants,
    and the worker sends artifacts, not statuses."""
    suggestion, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])

    response = client.put(f"/api/v1/suggestions/{suggestion.id}", json={"status": "pending"})

    assert response.status_code == 409
    assert _status(db, suggestion.id) == "approved"


def test_a_bulk_review_reports_an_approved_plan_row_as_skipped(
    client, db, site, articles, monkeypatch
):
    suggestion, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])

    body = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [suggestion.id], "status": "rejected"},
    ).json()

    assert body["reviewed"] == []
    assert body["skipped"] == [suggestion.id]


def test_a_selected_row_bound_to_no_plan_is_still_reviewable(client, db, site, articles):
    suggestion = _suggestion(db, site, *articles)

    response = client.put(f"/api/v1/suggestions/{suggestion.id}", json={"status": "pending"})

    assert response.status_code == 200
    assert _status(db, suggestion.id) == "pending"


# -- queueing decides nothing ----------------------------------------------


def test_queueing_a_site_without_an_approved_plan_is_refused(client, db, site, articles):
    """The handcrafted request the old endpoint would have honoured.

    It published everything selected, so a caller who never prepared anything
    could still write to a customer's site.
    """
    _suggestion(db, site, *articles)

    response = client.post(f"/api/v1/publish/{site.id}")

    assert response.status_code == 409
    assert "no approved publication plan" in response.json()["detail"]


def test_queueing_a_site_with_an_approved_plan_is_accepted(client, db, site, articles, monkeypatch):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])

    assert client.post(f"/api/v1/publish/{site.id}").status_code == 202


def test_queueing_can_name_only_the_visible_approved_plans(
    client, db, site, articles, monkeypatch
):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])
    captured = {}

    def enqueue(_db, _site_id, _kind, _task, job_timeout, task_kwargs=None):
        captured.update(task_kwargs or {})
        return SimpleNamespace(id=123, queue_job_id="publication-123")

    monkeypatch.setattr("app.api.routes.publish.enqueue_job", enqueue)

    response = client.post(
        f"/api/v1/publish/{site.id}",
        json={"plan_ids": [plan["id"]]},
    )

    assert response.status_code == 202
    assert captured == {"plan_ids": [plan["id"]]}


def test_database_admin_key_is_not_a_human_approval_identity(
    client, db, site, articles, monkeypatch
):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    original = app.dependency_overrides[require_api_key]
    app.dependency_overrides[require_api_key] = lambda: Principal(
        is_admin=True,
        source="db",
        key_id=7,
    )
    try:
        response = _approve(client, site, [plan], expect=401)
    finally:
        app.dependency_overrides[require_api_key] = original

    assert "operator-specific" in response.text


def test_pending_reports_selected_rows_and_approved_plans_apart(
    client, db, site, articles, monkeypatch
):
    _, plan = _prepared(client, db, site, articles, monkeypatch)
    _approve(client, site, [plan])

    row = next(
        entry
        for entry in client.get("/api/v1/publish/pending").json()["items"]
        if entry["site_id"] == site.id
    )

    assert (row["selected_suggestions"], row["approved_plans"]) == (0, 1)
    assert (row["site_name"], row["platform"]) == (site.name, "wordpress")


def test_pending_publication_is_cursor_paged_with_fleet_totals(client, db, site, articles):
    marker = uuid.uuid4().hex[:8]
    site.name = f"fleet-{marker}-one"
    _suggestion(db, site, *articles)
    other = Site(
        tenant_id=site.tenant_id,
        name=f"fleet-{marker}-two",
        base_url=f"https://fleet-{marker}.example.com",
        platform="wordpress",
        wp_username="editor",
    )
    db.add(other)
    db.flush()
    source = Article(
        site_id=other.id,
        external_id="source",
        url=f"{other.base_url}/source",
        title="source",
        content_text="source",
    )
    target = Article(
        site_id=other.id,
        external_id="target",
        url=f"{other.base_url}/target",
        title="target",
        content_text="target",
    )
    db.add_all([source, target])
    db.flush()
    _suggestion(db, other, source, target)

    first = client.get(
        "/api/v1/publish/pending",
        params={"limit": 1, "search": f"fleet-{marker}"},
    ).json()
    assert len(first["items"]) == 1
    assert first["next_cursor"] == first["items"][0]["site_id"]
    assert (first["total_sites"], first["total_selected_suggestions"]) == (2, 2)

    second = client.get(
        "/api/v1/publish/pending",
        params={
            "limit": 1,
            "cursor": first["next_cursor"],
            "search": f"fleet-{marker}",
            "include_totals": False,
        },
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert second["items"][0]["site_id"] != first["items"][0]["site_id"]
    assert second["total_sites"] is None


def test_async_preparation_is_owned_and_queued(client, site, monkeypatch):
    captured = {}

    def enqueue(*args, **kwargs):
        captured.update({"args": args, **kwargs})
        return SimpleNamespace(id=42, queue_job_id="prepare-job", requested_by=kwargs["requested_by"])

    monkeypatch.setattr("app.api.routes.publish.enqueue_job", enqueue)

    response = client.post(f"/api/v1/publish/{site.id}/plans/prepare-async")

    assert response.status_code == 202
    assert response.json() == {"job_id": "prepare-job", "job_run_id": 42}
    assert captured["args"][2] == "publication_preparation"
    assert captured["requested_by"]
    assert captured["task_kwargs"] == {"max_articles": 10}


def test_exact_html_is_loaded_separately(client, db, site, articles, monkeypatch):
    _, plan = _prepared(client, db, site, articles, monkeypatch)

    response = client.get(f"/api/v1/publish/{site.id}/plans/{plan['id']}/html")

    assert response.status_code == 200
    assert response.json() == {
        "id": plan["id"],
        "plan_hash": plan["plan_hash"],
        "original_html": plan["original_html"],
        "updated_html": plan["updated_html"],
    }
