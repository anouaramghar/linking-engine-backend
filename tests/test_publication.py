"""The publication worker: send approved artifacts, decide nothing.

Every test here is really one assertion in different clothes — *the bytes that
reach WordPress are the bytes a named human approved*. So the interesting cases
are the ones where something changed between approval and the write: the article
moved, the stored artifact was tampered with, a second worker got there first, a
reviewer selected more rows, the model came back with a better anchor. In every
one of them the answer is the same: publish exactly what was approved, or publish
nothing at all.

The connector is stubbed; claiming and locking run against real PostgreSQL.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.connectors.base import StalePlanError
from app.db import SessionLocal
from app.models import (
    Alert,
    Article,
    ExternalLinkPolicy,
    JobRun,
    PublicationPlan,
    Suggestion,
    SuggestionEvent,
)
from app.services import job_service, publication_plan_service
from app.services.external_link_policy import recheck_external_suggestions_before_publication
from app.services.live_url import LiveURLChecker
from app.tasks import publication
from app.tasks.publication import publish_approved_plans

APPROVED_BEFORE = "<p>Before</p>"
APPROVED_AFTER = '<p>Before <a href="https://example.com/tgt">anchor</a></p>'
OPERATOR = "telegram:4242"


@pytest.fixture
def articles(db, site):
    src = Article(site_id=site.id, url=f"{site.base_url}/src", title="src", content_text="a")
    tgt = Article(site_id=site.id, url=f"{site.base_url}/tgt", title="tgt", content_text="b")
    db.add_all([src, tgt])
    db.commit()
    return src, tgt  # cascade-deleted with the site


def _suggestion(db, site, src, tgt, status="approved", score=0.9, anchor_text="anchor"):
    row = Suggestion(
        site_id=site.id,
        source_article_id=src.id,
        target_article_id=tgt.id,
        method="baseline_cosine",
        score=score,
        rank_score=score,
        status=status,
        anchor_text=anchor_text,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _prepare(
    db,
    site,
    source,
    suggestions,
    *,
    original=APPROVED_BEFORE,
    updated=APPROVED_AFTER,
    outcomes=None,
):
    """A prepared plan, exactly as `prepare_site` would have stored one."""
    plan = PublicationPlan(
        site_id=site.id,
        source_article_id=source.id,
        source_url=source.url,
        status="prepared",
        original_html=original,
        updated_html=updated,
        items=publication_plan_service.snapshot_items(
            suggestions, outcomes or ["inserted"] * len(suggestions)
        ),
    )
    plan.plan_hash = publication_plan_service.compute_plan_hash(plan)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _approve(db, site, plan, approved_by=OPERATOR):
    return publication_plan_service.approve_plans(
        db, site.id, [(plan.id, plan.plan_hash)], approved_by=approved_by
    )[0]


def _approved_plan(db, site, source, suggestions, **kwargs):
    return _approve(db, site, _prepare(db, site, source, suggestions, **kwargs))


def _stub_connector(monkeypatch, apply=None, outcome="written"):
    """A connector with exactly one method: the one that sends finished bytes.

    Deliberately not a full connector. If the worker ever reaches for
    `preview_links`, `apply_links`, or anything else that could re-render the
    edit, these tests fail with an AttributeError rather than quietly passing.
    """
    calls = []

    def apply_planned_edit(source, *, original_html, updated_html):
        calls.append(
            SimpleNamespace(
                source_id=source.id, original_html=original_html, updated_html=updated_html
            )
        )
        if apply is not None:
            return apply(source, original_html, updated_html) or outcome
        return outcome

    monkeypatch.setattr(
        publication,
        "get_connector",
        lambda site: SimpleNamespace(apply_planned_edit=apply_planned_edit),
    )
    return calls


def _core(result: dict) -> dict:
    """The three counters most of these tests are about, without the outcome split."""
    return {key: result[key] for key in ("applied", "failed", "skipped")}


def _status(db, suggestion_id):
    db.expire_all()
    return db.get(Suggestion, suggestion_id).status


def _plan_status(db, plan_id):
    db.expire_all()
    return db.get(PublicationPlan, plan_id).status


def _sources(db, site, target, count):
    """`count` selected suggestions, each from its own source article.

    Publication writes one article per plan, so suggestions sharing a source
    succeed or fail together. Giving each its own article is what lets a test
    fail exactly one of them.
    """
    made = []
    for _ in range(count):
        slug = uuid.uuid4().hex[:8]  # a test may call this twice on the same site
        source = Article(
            site_id=site.id,
            url=f"{site.base_url}/src-{slug}",
            title=f"src {slug}",
            content_text="a",
        )
        db.add(source)
        db.commit()
        suggestion = _suggestion(db, site, source, target)
        made.append((source, suggestion, _approved_plan(db, site, source, [suggestion])))
    return made


# -- the guarantee ---------------------------------------------------------


def test_publication_sends_the_approved_html_and_nothing_else(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    calls = _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id)

    assert _core(result) == {"applied": 1, "failed": 0, "skipped": 0}
    assert [(call.original_html, call.updated_html) for call in calls] == [
        (APPROVED_BEFORE, APPROVED_AFTER)
    ]
    assert _status(db, suggestion.id) == "applied"
    assert _plan_status(db, plan.id) == "applied"
    db.expire_all()
    stored = db.get(PublicationPlan, plan.id)
    assert stored.approved_by == OPERATOR
    assert stored.approved_hash == stored.plan_hash
    assert stored.applied_at is not None


def test_policy_retired_tavily_target_stales_plan_before_network_access(
    db, site, articles, monkeypatch
):
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=articles[0].id,
        target_article_id=None,
        external_url="https://blocked.example/tgt",
        external_title="Blocked Tavily result",
        provider="tavily",
        method="external_search",
        score=0.9,
        rank_score=0.9,
        status="approved",
        anchor_text="anchor",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    db.add(
        ExternalLinkPolicy(
            site_id=site.id,
            blocklist_domains=["blocked.example"],
            updated_by=OPERATOR,
        )
    )
    db.commit()
    calls = _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id)

    assert calls == []
    assert _core(result) == {"applied": 0, "failed": 0, "skipped": 1}
    db.expire_all()
    retired = db.get(Suggestion, suggestion.id)
    assert (retired.status, retired.publication_plan_id) == ("expired", None)
    assert _plan_status(db, plan.id) == "stale"


def test_dead_external_target_stales_plan_before_wordpress_write(db, site, articles, monkeypatch):
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=articles[0].id,
        target_article_id=None,
        external_url="https://reference.example/dead",
        external_title="Dead source",
        provider="tavily",
        method="external_search",
        score=0.9,
        rank_score=0.9,
        status="approved",
        anchor_text="anchor",
    )
    db.add(suggestion)
    db.add(
        ExternalLinkPolicy(
            site_id=site.id,
            external_links_enabled=True,
            min_trust_score=0,
        )
    )
    db.commit()
    db.refresh(suggestion)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    calls = _stub_connector(monkeypatch)
    checker = LiveURLChecker(
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(410))),
        validator=lambda _url: None,
    )

    def dead_live_gate(session, source_site, **kwargs):
        return recheck_external_suggestions_before_publication(
            session,
            source_site,
            statuses=kwargs["statuses"],
            actor=kwargs["actor"],
            checker=checker,
            publication_plan_ids=kwargs["publication_plan_ids"],
        )

    monkeypatch.setattr(
        publication,
        "recheck_external_suggestions_before_publication",
        dead_live_gate,
    )

    result = publish_approved_plans(site.id)

    assert calls == []
    assert result["live_url_checked"] == 1
    assert result["live_url_expired"] == 1
    assert _status(db, suggestion.id) == "expired"
    assert _plan_status(db, plan.id) == "stale"


def test_each_plan_is_live_checked_at_its_own_publication_boundary(db, site, articles, monkeypatch):
    """The second target dies while the first plan is being written.

    A gate that ran once for the whole batch had already blessed this target
    before the first WordPress call, the optional inter-request pause, and every
    other plan's network work. By the time its own write left, the observation
    was minutes old and wrong, and LinkMesh published a dead link. The check
    belongs to the plan, not to the run.
    """
    sources = []
    suggestions = []
    for index in range(2):
        source = Article(
            site_id=site.id,
            url=f"{site.base_url}/live-src-{index}",
            title=f"src {index}",
            content_text="a",
        )
        db.add(source)
        db.commit()
        suggestion = Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=None,
            external_url=f"https://reference.example/target-{index}",
            external_title=f"Target {index}",
            provider="tavily",
            method="external_search",
            score=0.9,
            rank_score=0.9,
            status="approved",
            anchor_text="anchor",
        )
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)
        sources.append(source)
        suggestions.append(suggestion)
    db.add(ExternalLinkPolicy(site_id=site.id, external_links_enabled=True, min_trust_score=0))
    db.commit()
    plans = [
        _approved_plan(db, site, source, [suggestion])
        for source, suggestion in zip(sources, suggestions, strict=True)
    ]

    second_alive = True
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url.endswith("/target-1") and not second_alive:
            return httpx.Response(410)
        return httpx.Response(200)

    checker = LiveURLChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validator=lambda _url: None,
    )

    def scoped_live_gate(session, source_site, **kwargs):
        return recheck_external_suggestions_before_publication(
            session,
            source_site,
            statuses=kwargs["statuses"],
            actor=kwargs["actor"],
            checker=checker,
            publication_plan_ids=kwargs["publication_plan_ids"],
        )

    monkeypatch.setattr(
        publication,
        "recheck_external_suggestions_before_publication",
        scoped_live_gate,
    )

    def kill_the_second_target(_source, _original_html, _updated_html):
        nonlocal second_alive
        second_alive = False

    calls = _stub_connector(monkeypatch, apply=kill_the_second_target)

    result = publish_approved_plans(site.id)

    # The write that matters is the one that did not happen.
    assert [call.source_id for call in calls] == [sources[0].id]
    assert requested == [
        "https://reference.example/target-0",
        "https://reference.example/target-1",
    ]
    assert _core(result) == {"applied": 1, "failed": 0, "skipped": 1}
    assert (result["live_url_checked"], result["live_url_passed"], result["live_url_expired"]) == (
        2,
        1,
        1,
    )
    assert _status(db, suggestions[0].id) == "applied"
    assert _plan_status(db, plans[0].id) == "applied"
    assert _status(db, suggestions[1].id) == "expired"
    assert _plan_status(db, plans[1].id) == "stale"
    assert (
        db.scalar(
            select(SuggestionEvent).where(
                SuggestionEvent.suggestion_id == suggestions[1].id,
                SuggestionEvent.event_type == "live_url_expired",
            )
        )
        is not None
    )


def test_a_site_with_only_selected_suggestions_publishes_nothing(db, site, articles, monkeypatch):
    """The whole point. 'approved' on a suggestion is a selection, not consent."""
    suggestion = _suggestion(db, site, *articles)
    calls = _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id)

    assert calls == []
    assert _core(result) == {"applied": 0, "failed": 0, "skipped": 0}
    assert _status(db, suggestion.id) == "approved"


def test_the_worker_asks_no_model_and_renders_nothing(db, site, articles, monkeypatch):
    """No OpenRouter, no placement generation, no preview_links.

    A suggestion with no anchor used to reach publication and have one generated
    for it there, minutes after the last human looked at the article. The stub
    connector has no rendering method at all, so any attempt would raise.
    """
    suggestion = _suggestion(db, site, *articles, anchor_text=None)
    _approved_plan(db, site, articles[0], [suggestion], outcomes=["block"])
    monkeypatch.setattr(
        publication_plan_service.placement_service,
        "generate",
        lambda *_args, **_kwargs: pytest.fail("publication generated a placement"),
    )
    monkeypatch.setattr(
        publication_plan_service,
        "generate_missing_placements",
        lambda *_args, **_kwargs: pytest.fail("publication ran the placement preflight"),
    )
    calls = _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id)

    assert result["block"] == 1
    assert [call.updated_html for call in calls] == [APPROVED_AFTER]


def test_a_suggestion_selected_after_approval_is_not_swept_in(db, site, articles, monkeypatch):
    """The approved artifact is a closed set. Adding to it silently would publish
    a link nobody approved, on an article somebody did."""
    src, tgt = articles
    approved_row = _suggestion(db, site, src, tgt)
    _approved_plan(db, site, src, [approved_row])
    late = Article(site_id=site.id, url=f"{site.base_url}/late", title="late", content_text="c")
    db.add(late)
    db.commit()
    latecomer = _suggestion(db, site, src, late)
    _stub_connector(monkeypatch)

    publish_approved_plans(site.id)

    assert _status(db, approved_row.id) == "applied"
    assert _status(db, latecomer.id) == "approved"
    assert db.get(Suggestion, latecomer.id).publication_plan_id is None


def test_the_stored_item_outcomes_are_copied_rather_than_re_derived(
    db, site, articles, monkeypatch
):
    """ "applied" cannot answer the question in-text placement exists to answer.

    The outcome an operator was shown is the outcome recorded, because nothing
    re-decides it at write time any more.
    """
    src, tgt = articles
    other = Article(site_id=site.id, url=f"{site.base_url}/o", title="o", content_text="c")
    db.add(other)
    db.commit()
    in_text = _suggestion(db, site, src, tgt, score=0.95)
    appended = _suggestion(db, site, src, other, score=0.60)
    _approved_plan(db, site, src, [in_text, appended], outcomes=["inserted", "block"])
    _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id)

    assert (result["inserted"], result["block"], result["already_present"]) == (1, 1, 0)
    db.expire_all()
    assert db.get(Suggestion, in_text.id).publish_outcome == "inserted"
    assert db.get(Suggestion, appended.id).publish_outcome == "block"


# -- drift: the article changed after approval ------------------------------


def test_source_drift_writes_nothing_and_frees_the_rows_for_a_new_plan(
    db, site, articles, monkeypatch
):
    """A changed article invalidates the rendering, not the editorial decision.

    So: no write, no RQ retry (a retry would only re-read the same changed
    article), the plan is stale with a reason, the suggestions go back to being
    merely selected, and an alert tells a human to prepare a new plan.
    """
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    _stub_connector(
        monkeypatch,
        apply=lambda *_args: (_ for _ in ()).throw(StalePlanError("post 10 was rewritten")),
    )

    result = publish_approved_plans(site.id)

    assert _core(result) == {"applied": 0, "failed": 0, "skipped": 1}
    db.expire_all()
    stale = db.get(PublicationPlan, plan.id)
    assert stale.status == "stale"
    assert "rewritten" in stale.failure_reason
    assert stale.invalidated_at is not None
    revived = db.get(Suggestion, suggestion.id)
    assert (revived.status, revived.publication_plan_id) == ("approved", None)
    alert = db.scalar(select(Alert).where(Alert.kind == "publication_plan_stale"))
    assert alert is not None


def test_a_stale_plan_can_be_prepared_again(db, site, articles, monkeypatch):
    """The active-plan index only reserves live snapshots, so a stale one steps
    aside for its replacement rather than blocking the article for ever."""
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    _stub_connector(
        monkeypatch, apply=lambda *_args: (_ for _ in ()).throw(StalePlanError("moved"))
    )
    publish_approved_plans(site.id)

    db.expire_all()  # the worker cleared the link on its own session
    replacement = _approved_plan(
        db,
        site,
        articles[0],
        [suggestion],
        original="<p>Rewritten</p>",
        updated="<p>Rewritten!</p>",
    )
    calls = _stub_connector(monkeypatch)
    publish_approved_plans(site.id)

    assert [call.updated_html for call in calls] == ["<p>Rewritten!</p>"]
    assert _plan_status(db, plan.id) == "stale"
    assert _plan_status(db, replacement.id) == "applied"


# -- tampering: the artifact changed after approval -------------------------


def test_a_mutated_artifact_is_refused_before_any_network_access(db, site, articles, monkeypatch):
    """The last line of defence, and the reason the hash is recomputed.

    An UPDATE that rewrote `updated_html` would rewrite `plan_hash` in the same
    statement just as easily, so the stored hash proves nothing on its own. What
    the operator bound themselves to is `approved_hash`, and the artifact has to
    still hash to that.
    """
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    db.execute(
        update(PublicationPlan)
        .where(PublicationPlan.id == plan.id)
        .values(updated_html='<p>Before <a href="https://evil.example/">anchor</a></p>')
    )
    db.commit()
    calls = _stub_connector(monkeypatch)

    with pytest.raises(job_service.NonRetryableTaskError, match="does not match its stored hash"):
        publish_approved_plans(site.id)

    assert calls == []
    assert _plan_status(db, plan.id) == "failed"
    db.expire_all()
    failed = db.get(Suggestion, suggestion.id)
    assert failed.status == "failed"
    # The link survives: an integrity failure is exactly when "which rows fed
    # this artifact" has to remain answerable.
    assert failed.publication_plan_id == plan.id
    alert = db.scalar(select(Alert).where(Alert.kind == "publication_plan_failed"))
    assert alert is not None


def test_an_artifact_that_no_longer_matches_the_approved_hash_is_refused(
    db, site, articles, monkeypatch
):
    """Re-hashed consistently, so `plan_hash` agrees — and `approved_hash` does not."""
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    db.expire_all()
    tampered = db.get(PublicationPlan, plan.id)
    tampered.updated_html = '<p>Before <a href="https://evil.example/">anchor</a></p>'
    tampered.plan_hash = publication_plan_service.compute_plan_hash(tampered)
    db.commit()
    calls = _stub_connector(monkeypatch)

    with pytest.raises(job_service.NonRetryableTaskError, match="no longer matches the artifact"):
        publish_approved_plans(site.id)

    assert calls == []
    assert _plan_status(db, plan.id) == "failed"


# -- concurrency and retries ------------------------------------------------


def test_a_retry_after_a_successful_remote_write_does_not_write_twice(
    db, site, articles, monkeypatch
):
    """The crash-between-POST-and-commit case, end to end.

    The connector recognises its own earlier write, so the retry finalizes the
    database state rather than appending the same links a second time.
    """
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    posts = []

    def apply(source, original_html, updated_html):
        posts.append(updated_html)
        raise RuntimeError("connection reset after the write landed")

    _stub_connector(monkeypatch, apply=apply)
    with pytest.raises(RuntimeError, match="1 publication link"):
        publish_approved_plans(site.id)

    assert _plan_status(db, plan.id) == "approved"  # retryable, not stuck

    _stub_connector(monkeypatch, outcome="already_applied")
    retry = publish_approved_plans(site.id)

    assert retry["applied"] == 1
    assert len(posts) == 1
    assert _plan_status(db, plan.id) == "applied"
    assert _status(db, suggestion.id) == "applied"


def test_two_sequential_workers_cannot_publish_the_same_plan_twice(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    calls = _stub_connector(monkeypatch)

    first = publish_approved_plans(site.id)
    second = publish_approved_plans(site.id)

    assert first["applied"] == 1
    assert _core(second) == {"applied": 0, "failed": 0, "skipped": 0}
    assert len(calls) == 1
    assert _plan_status(db, plan.id) == "applied"


def test_two_concurrent_workers_cannot_publish_the_same_plan_twice(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    _approved_plan(db, site, articles[0], [suggestion])
    calls = _stub_connector(monkeypatch)
    both_loaded = Barrier(2)
    original_load = publication.load_approved_plans

    def synchronized_load(session, site_id, plan_ids=None):
        plans = original_load(session, site_id, plan_ids=plan_ids)
        both_loaded.wait(timeout=5)
        return plans

    monkeypatch.setattr(publication, "load_approved_plans", synchronized_load)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish_approved_plans(site.id), range(2)))

    assert len(calls) == 1
    assert sum(result["applied"] for result in results) == 1
    assert sum(result["skipped"] for result in results) == 1


def test_a_scoped_worker_leaves_other_approved_plans_untouched(db, site, articles, monkeypatch):
    first = _suggestion(db, site, *articles)
    first_plan = _approved_plan(db, site, articles[0], [first])
    second_source = Article(
        site_id=site.id,
        url=f"{site.base_url}/second",
        title="second",
        content_text="second",
    )
    db.add(second_source)
    db.commit()
    second = _suggestion(db, site, second_source, articles[1])
    second_plan = _approved_plan(db, site, second_source, [second])
    calls = _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id, plan_ids=[second_plan.id])

    assert result["applied"] == 1
    assert [call.source_id for call in calls] == [second_source.id]
    assert _plan_status(db, first_plan.id) == "approved"
    assert _plan_status(db, second_plan.id) == "applied"


def test_a_scoped_worker_checks_only_external_links_in_its_selected_plan(
    db, site, articles, monkeypatch
):
    first_source = articles[0]
    second_source = Article(
        site_id=site.id,
        url=f"{site.base_url}/second-external",
        title="second external",
        content_text="second external",
    )
    db.add(second_source)
    db.add(
        ExternalLinkPolicy(
            site_id=site.id,
            external_links_enabled=True,
            min_trust_score=0,
            blocklist_domains=["blocked.example"],
        )
    )
    db.commit()

    first = Suggestion(
        site_id=site.id,
        source_article_id=first_source.id,
        external_url="https://blocked.example/unrelated",
        external_title="Unrelated blocked target",
        provider="tavily",
        method="external_search",
        score=0.9,
        rank_score=0.9,
        status="approved",
        anchor_text="unrelated",
    )
    second = Suggestion(
        site_id=site.id,
        source_article_id=second_source.id,
        external_url="https://reference.example/selected",
        external_title="Selected live target",
        provider="tavily",
        method="external_search",
        score=0.9,
        rank_score=0.9,
        status="approved",
        anchor_text="selected",
    )
    db.add_all([first, second])
    db.commit()
    db.refresh(first)
    db.refresh(second)
    first_plan = _approved_plan(db, site, first_source, [first])
    second_plan = _approved_plan(db, site, second_source, [second])

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(204)

    checker = LiveURLChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validator=lambda _url: None,
    )

    def scoped_live_gate(session, source_site, **kwargs):
        return recheck_external_suggestions_before_publication(
            session,
            source_site,
            statuses=kwargs["statuses"],
            actor=kwargs["actor"],
            checker=checker,
            publication_plan_ids=kwargs["publication_plan_ids"],
        )

    monkeypatch.setattr(
        publication,
        "recheck_external_suggestions_before_publication",
        scoped_live_gate,
    )
    calls = _stub_connector(monkeypatch)

    result = publish_approved_plans(site.id, plan_ids=[second_plan.id])

    assert result["applied"] == 1
    assert result["policy_expired"] == 0
    assert result["live_url_checked"] == 1
    assert requested == ["https://reference.example/selected"]
    assert [call.source_id for call in calls] == [second_source.id]
    assert _status(db, first.id) == "approved"
    assert _plan_status(db, first_plan.id) == "approved"
    assert _plan_status(db, second_plan.id) == "applied"
    assert (
        db.scalar(
            select(SuggestionEvent).where(
                SuggestionEvent.suggestion_id == first.id,
                SuggestionEvent.event_type.in_(
                    ("policy_expired", "live_url_checked", "live_url_expired")
                ),
            )
        )
        is None
    )


def test_a_plan_invalidated_after_the_batch_read_is_skipped(db, site, articles, monkeypatch):
    """The status is re-read inside the row lock, not trusted from the batch read.

    Two plans, and the second is taken by another worker while the first is being
    written. Without the re-read this run would send an edit that has already
    been published, or one an operator has just invalidated.
    """
    made = _sources(db, site, articles[1], 2)
    second_plan_id = made[1][2].id

    taken = []

    def take_the_second(*_args):
        # Only while the *first* plan is being written: reaching into the row the
        # worker is holding open would simply deadlock against its own lock.
        if taken:
            return
        taken.append(True)
        other = SessionLocal()
        try:
            other.execute(
                update(PublicationPlan)
                .where(PublicationPlan.id == second_plan_id)
                .values(status="applied")
            )
            other.commit()
        finally:
            other.close()

    calls = _stub_connector(monkeypatch, apply=take_the_second)
    result = publish_approved_plans(site.id)

    assert len(calls) == 1  # only the first plan reached the connector
    assert _core(result) == {"applied": 1, "failed": 0, "skipped": 1}


def test_a_review_cannot_land_on_top_of_a_publish_in_flight(
    client, db, site, articles, monkeypatch
):
    """While the write runs, the plan's suggestions are row-locked.

    A reviewer's guarded update blocks instead of rewriting a row that is in the
    middle of becoming a live link.
    """
    suggestion = _suggestion(db, site, *articles)
    _approved_plan(db, site, articles[0], [suggestion])
    outcome = {}

    def apply(_source, _original, _updated):
        other = SessionLocal()
        try:
            other.execute(text("SET lock_timeout = '200ms'"))
            try:
                other.execute(
                    update(Suggestion)
                    .where(Suggestion.id == suggestion.id)
                    .values(status="rejected")
                )
                outcome["reject"] = "landed"
            except OperationalError:
                outcome["reject"] = "blocked"
            other.rollback()
        finally:
            other.close()

    _stub_connector(monkeypatch, apply=apply)
    result = publish_approved_plans(site.id)

    assert outcome["reject"] == "blocked"
    assert result["applied"] == 1
    assert _status(db, suggestion.id) == "applied"


def test_failed_publication_rolls_back_to_approved_for_retry(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    _stub_connector(
        monkeypatch, apply=lambda *_args: (_ for _ in ()).throw(RuntimeError("WP returned 500"))
    )

    with pytest.raises(RuntimeError, match="1 publication link"):
        publish_approved_plans(site.id)

    assert _plan_status(db, plan.id) == "approved"
    assert _status(db, suggestion.id) == "approved"

    _stub_connector(monkeypatch)
    assert publish_approved_plans(site.id)["applied"] == 1
    assert _plan_status(db, plan.id) == "applied"


# -- repeated failures (finding 8) -----------------------------------------


def _always_fails(monkeypatch, message="WP stayed unavailable"):
    _stub_connector(monkeypatch, apply=lambda *_args: (_ for _ in ()).throw(RuntimeError(message)))


def test_a_plan_that_keeps_failing_is_retired_with_its_suggestions(db, site, articles, monkeypatch):
    """Otherwise a permanently broken article is retried by every run, for ever.

    A deleted post, a plugin lock, a revoked password: the write rolls back each
    time, so nothing accumulates and nothing ever says so.
    """
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 2)
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    _always_fails(monkeypatch)

    with pytest.raises(RuntimeError, match="1 publication link"):
        publish_approved_plans(site.id)
    db.expire_all()
    assert db.get(Suggestion, suggestion.id).publish_attempts == 1
    assert _plan_status(db, plan.id) == "approved"  # one bad night is not terminal

    with pytest.raises(job_service.NonRetryableTaskError, match="quarantined after 2 attempts"):
        publish_approved_plans(site.id)

    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert stored.status == "failed"
    assert "WP stayed unavailable" in stored.publish_error
    attempts = [event for event in stored.events if event.event_type == "publish_attempt_failed"]
    assert [event.details["attempt"] for event in attempts] == [1, 2]
    assert [event.details["terminal"] for event in attempts] == [False, True]
    assert all("WP stayed unavailable" in event.details["reason"] for event in attempts)
    assert _plan_status(db, plan.id) == "failed"


def test_a_retired_plan_is_not_picked_up_again(db, site, articles, monkeypatch):
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 1)
    suggestion = _suggestion(db, site, *articles)
    _approved_plan(db, site, articles[0], [suggestion])
    _always_fails(monkeypatch)
    with pytest.raises(RuntimeError):
        publish_approved_plans(site.id)
    assert _status(db, suggestion.id) == "failed"

    calls = _stub_connector(monkeypatch)
    assert _core(publish_approved_plans(site.id)) == {"applied": 0, "failed": 0, "skipped": 0}
    assert calls == []


def test_a_successful_publish_clears_the_failure_history(db, site, articles, monkeypatch):
    """The counter is for articles that never work, not for a bad night."""
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 5)
    suggestion = _suggestion(db, site, *articles)
    _approved_plan(db, site, articles[0], [suggestion])
    _always_fails(monkeypatch)
    with pytest.raises(RuntimeError):
        publish_approved_plans(site.id)

    _stub_connector(monkeypatch)
    publish_approved_plans(site.id)

    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert stored.status == "applied"
    assert stored.publish_attempts == 0
    assert stored.publish_error is None


def test_re_selecting_a_failed_suggestion_frees_it_from_its_old_plan(
    client, db, site, articles, monkeypatch
):
    """Explicit reselection is the documented recovery from a failed plan.

    It clears the link so the row can be prepared again, while the plan row
    itself stays readable as history.
    """
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 1)
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    _always_fails(monkeypatch)
    with pytest.raises(RuntimeError):
        publish_approved_plans(site.id)

    for status in ("pending", "approved"):  # what Undo then Select sends
        response = client.put(f"/api/v1/suggestions/{suggestion.id}", json={"status": status})
        assert response.status_code == 200, response.text

    db.expire_all()
    revived = db.get(Suggestion, suggestion.id)
    assert (revived.status, revived.publication_plan_id, revived.publish_attempts) == (
        "approved",
        None,
        0,
    )
    assert db.get(PublicationPlan, plan.id) is not None  # the artifact is still readable


# -- durable accounting ----------------------------------------------------


def test_retry_preserves_original_total_and_cumulative_applied(db, site, articles, monkeypatch):
    made = _sources(db, site, articles[1], 10)
    final_plan_id = made[-1][2].id
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    monkeypatch.setattr(job_service, "get_current_job", lambda: SimpleNamespace(retries_left=1))

    def fail_last(source, _original, _updated):
        if source.id == made[-1][0].id:
            raise RuntimeError("transient WordPress failure")

    _stub_connector(monkeypatch, apply=fail_last)
    with pytest.raises(RuntimeError, match="1 publication link"):
        publish_approved_plans(site.id, job_run_id=run.id)

    db.expire_all()
    first_attempt = dict(db.get(JobRun, run.id).progress)
    assert first_attempt == {
        "stage": "publishing",
        "applied": 9,
        "failed": 1,
        "skipped": 0,
        "total": 10,
        "attempt_skipped": 0,
        "attempt_failed": 1,
        "attempt_failures": 1,
        "failure_state": "retrying",
        "inserted": 9,
        "block": 0,
        "already_present": 0,
    }
    assert _plan_status(db, final_plan_id) == "approved"

    _stub_connector(monkeypatch)
    result = publish_approved_plans(site.id, job_run_id=run.id)

    assert _core(result) == {"applied": 10, "failed": 0, "skipped": 0}
    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "succeeded"
    assert stored.attempts == 2
    assert stored.progress["total"] == 10
    # cumulative like `applied`: nine from the first attempt, one from the retry
    assert stored.progress["inserted"] == 10


def test_a_batch_is_the_links_inside_approved_plans_not_the_whole_backlog(
    db, site, articles, monkeypatch
):
    """A denominator of "everything selected" would describe work this run was
    never going to do — and was exactly the old, unsafe cohort."""
    src, tgt = articles
    approved_row = _suggestion(db, site, src, tgt)
    _approved_plan(db, site, src, [approved_row])
    _sources(db, site, tgt, 0)
    unprepared = Article(site_id=site.id, url=f"{site.base_url}/u", title="u", content_text="c")
    db.add(unprepared)
    db.commit()
    _suggestion(db, site, unprepared, tgt)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    _stub_connector(monkeypatch)

    publish_approved_plans(site.id, job_run_id=run.id)

    db.expire_all()
    assert db.get(JobRun, run.id).progress["total"] == 1


def test_final_publication_failure_records_job_and_alerts(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    _approved_plan(db, site, articles[0], [suggestion])
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    monkeypatch.setattr(job_service, "get_current_job", lambda: SimpleNamespace(retries_left=0))
    _always_fails(monkeypatch)

    with pytest.raises(RuntimeError, match="1 publication link"):
        publish_approved_plans(site.id, job_run_id=run.id)

    db.expire_all()
    assert _status(db, suggestion.id) == "approved"
    stored = db.get(JobRun, run.id)
    assert stored.status == "failed"
    assert stored.progress["failure_state"] == "terminal"
    alert = db.scalar(select(Alert).where(Alert.site_id == site.id, Alert.kind == "job_failed"))
    assert alert.subject == "LinkMesh publication job failed"


def test_a_commit_that_fails_does_not_count_its_links_as_applied(db, site, articles, monkeypatch):
    """The plan rolls back to 'approved', so the counters have to roll back too.

    Counting a link the moment the connector returns means a failed commit
    reports the same link as applied *and* failed, and the retry then resumes
    from an 'applied' total that includes a link nobody wrote.
    """
    suggestion = _suggestion(db, site, *articles)
    plan = _approved_plan(db, site, articles[0], [suggestion])
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    _stub_connector(monkeypatch)
    _commit_fails_after(monkeypatch, allowed=1)  # the batch-open commit survives

    with pytest.raises(RuntimeError, match="1 publication link"):
        publish_approved_plans(site.id, job_run_id=run.id)

    db.expire_all()
    progress = db.get(JobRun, run.id).progress
    assert (progress["applied"], progress["failed"]) == (0, 1)
    assert _plan_status(db, plan.id) == "approved"
    assert db.get(Suggestion, suggestion.id).publish_attempts == 0


def _commit_fails_after(monkeypatch, allowed: int):
    """Break the publication session's commit once it has allowed `allowed` of them.

    `_publish_approved_plans` opens its work session first and its progress
    session second, so wrapping the first one puts the failure exactly on the
    commit that is supposed to make a write durable.
    """
    real_factory = publication.SessionLocal
    opened = []

    def factory():
        session = real_factory()
        opened.append(session)
        if len(opened) == 1:
            original, calls = session.commit, []

            def commit():
                calls.append(1)
                if len(calls) > allowed:
                    raise RuntimeError("server closed the connection during commit")
                return original()

            session.commit = commit
        return session

    monkeypatch.setattr(publication, "SessionLocal", factory)
