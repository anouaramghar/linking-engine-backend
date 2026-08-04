"""Safe publication (Phase 0, finding 5): claim, per-article serialization, reject
races, and retry semantics. Connector is stubbed; locking runs against real PostgreSQL."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.connectors.base import LinkPreview
from app.db import SessionLocal
from app.models import Alert, Article, JobRun, Suggestion
from app.services import job_service, placement_service
from app.tasks import publication
from app.tasks.publication import publish_approved


@pytest.fixture
def articles(db, site):
    src = Article(site_id=site.id, url=f"{site.base_url}/src", title="src", content_text="a")
    tgt = Article(site_id=site.id, url=f"{site.base_url}/tgt", title="tgt", content_text="b")
    db.add_all([src, tgt])
    db.commit()
    return src, tgt  # cascade-deleted with the site


def _suggestion(db, site, src, tgt, status="approved", score=0.9):
    s = Suggestion(
        site_id=site.id,
        source_article_id=src.id,
        target_article_id=tgt.id,
        method="baseline_cosine",
        score=score,
        status=status,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _stub_connector(monkeypatch, apply_link, outcome="inserted"):
    """A connector whose per-article write calls `apply_link` once per suggestion.

    Publication batches by source article, but these tests are about the claim,
    the locking and the retry semantics — all of which are per suggestion — so
    the stub keeps that shape and reports one outcome per suggestion.
    """

    def apply_links(suggestions, *, dry_run=False):
        for suggestion in suggestions:
            apply_link(suggestion)
        return [outcome] * len(suggestions)

    monkeypatch.setattr(
        publication, "get_connector", lambda site: SimpleNamespace(apply_links=apply_links)
    )


def _core(result: dict) -> dict:
    """The three counters most of these tests are about, without the outcome split."""
    return {key: result[key] for key in ("applied", "failed", "skipped")}


def _suggestions_on_distinct_sources(db, site, target, count):
    """`count` approved suggestions, each from its own source article.

    Publication writes one article in one edit, so suggestions sharing a source
    succeed or fail together. Progress accounting is per suggestion, and giving
    each its own article is what lets a test fail exactly one of them.
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
        made.append(_suggestion(db, site, source, target))
    return made


def _status(db, suggestion_id):
    db.expire_all()
    return db.get(Suggestion, suggestion_id).status


def test_publication_order_gives_a_contested_anchor_to_the_best_score(
    db, site, articles, monkeypatch
):
    """Publication order decides who wins a contested anchor, so it follows score.

    Two suggestions on one source article can want the same words. The first to
    reach the connector takes the in-text placement and the other falls back to
    the appended block, so the order of this batch is the arbitration. Created in
    the wrong order deliberately: by id, the weaker suggestion would go first.
    """
    src, tgt = articles
    other_target = Article(
        site_id=site.id, url=f"{site.base_url}/tgt2", title="tgt2", content_text="c"
    )
    db.add(other_target)
    db.commit()
    weaker = _suggestion(db, site, src, tgt, score=0.60)
    stronger = _suggestion(db, site, src, other_target, score=0.95)
    assert weaker.id < stronger.id

    published = []
    _stub_connector(monkeypatch, lambda s: published.append(s.id))

    publish_approved(site.id)

    assert published == [stronger.id, weaker.id]


def test_publish_applies_approved_suggestion(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    calls = []
    _stub_connector(monkeypatch, lambda s: calls.append(s.id))

    result = publish_approved(site.id)

    assert _core(result) == {"applied": 1, "failed": 0, "skipped": 0}
    assert calls == [suggestion.id]
    assert _status(db, suggestion.id) == "applied"
    assert db.get(Suggestion, suggestion.id).applied_at is not None


@pytest.mark.parametrize("inactive_index", [0, 1], ids=["source", "target"])
def test_publish_skips_inactive_pair_without_claiming(
    db, site, articles, monkeypatch, inactive_index
):
    articles[inactive_index].is_active = False
    db.commit()
    suggestion = _suggestion(db, site, *articles)
    calls = []
    _stub_connector(monkeypatch, lambda s: calls.append(s.id))

    result = publish_approved(site.id)

    assert _core(result) == {"applied": 0, "failed": 0, "skipped": 1}
    assert calls == []
    assert _status(db, suggestion.id) == "approved"


def test_double_publish_applies_exactly_once(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    calls = []
    _stub_connector(monkeypatch, lambda s: calls.append(s.id))

    first = publish_approved(site.id)
    second = publish_approved(site.id)

    assert first["applied"] == 1
    assert _core(second) == {"applied": 0, "failed": 0, "skipped": 0}
    assert calls == [suggestion.id]


def test_suggestion_rejected_after_batch_select_is_skipped(db, site, articles, monkeypatch):
    """The claim (approved -> applying) re-checks status; a reject that lands between
    the batch select and the claim must win."""
    src, tgt = articles
    # A third article, so the two suggestions are not each other's reverse:
    # publication drops the weaker half of a reciprocal pair, which would take
    # `second` out of the batch before the claim this test is about.
    other = Article(site_id=site.id, url=f"{site.base_url}/o", title="o", content_text="c")
    db.add(other)
    db.commit()
    first = _suggestion(db, site, src, tgt)
    second = _suggestion(db, site, tgt, other)
    calls = []

    def apply_link(s):
        calls.append(s.id)
        if s.id == first.id:  # reviewer rejects the not-yet-claimed second suggestion
            other = SessionLocal()
            try:
                other.execute(
                    update(Suggestion)
                    .where(
                        Suggestion.id == second.id,
                        Suggestion.status.notin_(["applying", "applied"]),
                    )
                    .values(status="rejected")
                )
                other.commit()
            finally:
                other.close()

    _stub_connector(monkeypatch, apply_link)
    result = publish_approved(site.id)

    assert _core(result) == {"applied": 1, "failed": 0, "skipped": 1}
    assert calls == [first.id]
    assert _status(db, first.id) == "applied"
    assert _status(db, second.id) == "rejected"


def test_reject_blocks_while_publish_is_in_flight(db, site, articles, monkeypatch):
    """While apply_link runs, the claim's row lock must hold off a concurrent reviewer:
    the guarded reject times out instead of landing on top of the publish."""
    suggestion = _suggestion(db, site, *articles)
    outcome = {}

    def apply_link(s):
        other = SessionLocal()
        try:
            other.execute(text("SET lock_timeout = '200ms'"))
            try:
                other.execute(
                    update(Suggestion)
                    .where(
                        Suggestion.id == s.id,
                        Suggestion.status.notin_(["applying", "applied"]),
                    )
                    .values(status="rejected")
                )
                outcome["reject"] = "landed"
            except OperationalError:
                outcome["reject"] = "blocked"
            other.rollback()
        finally:
            other.close()

    _stub_connector(monkeypatch, apply_link)
    result = publish_approved(site.id)

    assert outcome["reject"] == "blocked"
    assert result["applied"] == 1
    assert _status(db, suggestion.id) == "applied"


def test_failed_apply_rolls_back_to_approved_for_retry(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)

    def boom(s):
        raise RuntimeError("WP returned 500")

    _stub_connector(monkeypatch, boom)
    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id)

    assert _status(db, suggestion.id) == "approved"  # retryable, not stuck in 'applying'

    _stub_connector(monkeypatch, lambda s: None)
    retry = publish_approved(site.id)
    assert retry["applied"] == 1
    assert _status(db, suggestion.id) == "applied"


def test_retry_preserves_original_total_and_cumulative_applied(
    db, site, articles, monkeypatch
):
    suggestions = _suggestions_on_distinct_sources(db, site, articles[1], 10)
    final_suggestion_id = suggestions[-1].id
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service, "get_current_job", lambda: SimpleNamespace(retries_left=1)
    )

    def fail_last(suggestion):
        if suggestion.id == final_suggestion_id:
            raise RuntimeError("transient WordPress failure")

    _stub_connector(monkeypatch, fail_last)
    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

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

    retry_start = {}

    def observe_retry_start(_suggestion):
        with SessionLocal() as observer:
            retry_start.update(observer.get(JobRun, run.id).progress)

    _stub_connector(monkeypatch, observe_retry_start)
    result = publish_approved(site.id, job_run_id=run.id)

    assert retry_start["total"] == first_attempt["total"] == 10
    assert retry_start["applied"] == first_attempt["applied"] == 9
    assert retry_start["attempt_failures"] == first_attempt["attempt_failures"] == 1
    assert _core(result) == {"applied": 10, "failed": 0, "skipped": 0}
    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "succeeded"
    assert stored.attempts == 2
    assert stored.result == result
    assert stored.progress == {
        "stage": "publishing",
        "applied": 10,
        "failed": 0,
        "skipped": 0,
        "total": 10,
        "attempt_skipped": 0,
        "attempt_failed": 0,
        "attempt_failures": 1,
        "failure_state": None,
        # cumulative like `applied`: nine from the first attempt, one from the retry
        "inserted": 10,
        "block": 0,
        "already_present": 0,
    }
    assert result["inserted"] == 10


def test_retry_grows_total_for_suggestions_approved_between_attempts(
    db, site, articles, monkeypatch
):
    suggestions = _suggestions_on_distinct_sources(db, site, articles[1], 10)
    final_suggestion_id = suggestions[-1].id
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service, "get_current_job", lambda: SimpleNamespace(retries_left=1)
    )

    def fail_last(suggestion):
        if suggestion.id == final_suggestion_id:
            raise RuntimeError("transient WordPress failure")

    _stub_connector(monkeypatch, fail_last)
    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

    _suggestions_on_distinct_sources(db, site, articles[1], 5)
    _stub_connector(monkeypatch, lambda _suggestion: None)
    result = publish_approved(site.id, job_run_id=run.id)

    assert _core(result) == {"applied": 15, "failed": 0, "skipped": 0}
    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.result == result
    assert stored.progress["total"] == 15
    assert stored.progress["applied"] == 15


def test_retry_progress_exposes_latest_attempt_failure_count(
    db, site, articles, monkeypatch
):
    for _ in range(3):
        _suggestion(db, site, *articles)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service, "get_current_job", lambda: SimpleNamespace(retries_left=1)
    )
    _stub_connector(
        monkeypatch,
        lambda _suggestion: (_ for _ in ()).throw(RuntimeError("WordPress unavailable")),
    )

    with pytest.raises(RuntimeError, match="3 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    progress = db.get(JobRun, run.id).progress
    assert progress["failed"] == 3
    assert progress["attempt_failed"] == 3
    assert progress["attempt_failures"] == 3
    assert progress["failure_state"] == "retrying"


def test_all_skipped_publication_persists_final_progress(
    db, site, articles, monkeypatch
):
    articles[0].is_active = False
    db.commit()
    _suggestion(db, site, *articles)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    _stub_connector(
        monkeypatch,
        lambda _suggestion: pytest.fail("inactive suggestions must not be published"),
    )

    result = publish_approved(site.id, job_run_id=run.id)

    assert _core(result) == {"applied": 0, "failed": 0, "skipped": 1}
    db.expire_all()
    assert db.get(JobRun, run.id).progress == {
        "stage": "publishing",
        "applied": 0,
        "failed": 0,
        "skipped": 1,
        "total": 1,
        "attempt_skipped": 1,
        "attempt_failed": 0,
        "attempt_failures": 0,
        "failure_state": None,
        "inserted": 0,
        "block": 0,
        "already_present": 0,
    }


def test_final_publication_failure_records_job_and_alerts(
    db, site, articles, monkeypatch
):
    suggestion = _suggestion(db, site, *articles)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    monkeypatch.setattr(job_service, "get_current_job", lambda: SimpleNamespace(retries_left=0))
    _stub_connector(
        monkeypatch,
        lambda _suggestion: (_ for _ in ()).throw(RuntimeError("WP stayed unavailable")),
    )

    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    assert _status(db, suggestion.id) == "approved"
    stored = db.get(JobRun, run.id)
    assert stored.status == "failed"
    assert stored.progress == {
        "stage": "publishing",
        "applied": 0,
        "failed": 1,
        "skipped": 0,
        "total": 1,
        "attempt_skipped": 0,
        "attempt_failed": 1,
        "attempt_failures": 1,
        "failure_state": "terminal",
        "inserted": 0,
        "block": 0,
        "already_present": 0,
    }
    alert = db.scalar(select(Alert).where(Alert.site_id == site.id))
    assert alert.kind == "job_failed"
    assert alert.subject == "LinkMesh publication job failed"


# -- anchor arbitration (finding 10) ---------------------------------------


def test_a_contested_anchor_goes_to_the_better_bm25_not_the_better_cosine(
    db, site, articles, monkeypatch
):
    """Cosine had no part in choosing a hybrid row, so it must not break its ties.

    Both suggestions share one source article, which is the whole reason BM25 is
    usable here: it is not comparable across documents, and there is only one
    document. `Suggestion.score` stays cosine for every method so the queue keeps
    one meaning — it just stops being the thing that decides who gets the phrase.
    """
    src, tgt = articles
    other_target = Article(
        site_id=site.id, url=f"{site.base_url}/tgt2", title="tgt2", content_text="c"
    )
    db.add(other_target)
    db.commit()
    better_cosine = _suggestion(db, site, src, tgt, score=0.95)
    better_bm25 = _suggestion(db, site, src, other_target, score=0.60)
    better_cosine.score_components = {"bm25_score": 1.0}
    better_bm25.score_components = {"bm25_score": 9.0}
    db.commit()

    published = []
    _stub_connector(monkeypatch, lambda s: published.append(s.id))
    publish_approved(site.id)

    assert published == [better_bm25.id, better_cosine.id]


# -- reciprocal links (finding 11) -----------------------------------------


def _reverse_pair(db, site, articles, **kwargs):
    """A->B and B->A, the shape near-duplicate template pages always produce."""
    src, tgt = articles
    return _suggestion(db, site, src, tgt, **kwargs), _suggestion(db, site, tgt, src, **kwargs)


def test_a_pair_already_linked_the_other_way_is_expired_not_left_approved(
    db, site, articles, monkeypatch
):
    """A->B published, so B->A is a mutual link neither page needs.

    Left 'approved' it can never publish and never stops being approved, so
    /publish/pending counts it as awaiting publication for ever while every run
    reports success. Suppressing it has to be a decision, not a silent skip.
    """
    src, tgt = articles
    _suggestion(db, site, src, tgt, status="applied")
    back = _suggestion(db, site, tgt, src)

    published = []
    _stub_connector(monkeypatch, lambda s: published.append(s.id))
    result = publish_approved(site.id)

    assert published == []
    assert _core(result) == {"applied": 0, "failed": 0, "skipped": 0}
    assert _status(db, back.id) == "expired"


def test_both_directions_approved_together_publish_exactly_one(
    db, site, articles, monkeypatch
):
    """A bulk approval takes both halves; only one of them may be written.

    Which half is arbitrary, and the test says so rather than pretending
    otherwise: cosine is symmetric so both rows carry the identical score, and
    BM25 cannot compare two different source articles. What is required is that
    exactly one is written, and that a BM25 hint does not move the decision.
    """
    forward, backward = _reverse_pair(db, site, articles)
    backward.score_components = {"bm25_score": 99.0}
    db.commit()

    published = []
    _stub_connector(monkeypatch, lambda s: published.append(s.id))
    result = publish_approved(site.id)

    assert published == [forward.id]
    assert _core(result) == {"applied": 1, "failed": 0, "skipped": 0}
    # The loser is retired only after its winner commits, but inside this run:
    # publication must not report success with one impossible approved row left.
    assert _status(db, backward.id) == "expired"


def test_the_same_direction_suggested_twice_is_not_mistaken_for_a_reciprocal(
    db, site, articles, monkeypatch
):
    """Two rows for A->B are one link proposed twice, not two directions.

    The reciprocal gate keys on the unordered pair, so collapsing on that alone
    would silently drop every duplicate suggestion in the batch.
    """
    src, tgt = articles
    first = _suggestion(db, site, src, tgt)
    second = _suggestion(db, site, src, tgt)

    published = []
    _stub_connector(monkeypatch, lambda s: published.append(s.id))
    publish_approved(site.id)

    assert sorted(published) == sorted([first.id, second.id])


# -- repeated failures (finding 8) -----------------------------------------


def _always_fails(monkeypatch, message="WP stayed unavailable"):
    _stub_connector(
        monkeypatch, lambda _s: (_ for _ in ()).throw(RuntimeError(message)))


def test_a_suggestion_that_keeps_failing_is_quarantined(db, site, articles, monkeypatch):
    """Otherwise a permanently broken row is retried by every run, for ever.

    A deleted post, a plugin lock, a revoked password: the claim rolls back to
    'approved' each time, so nothing accumulates and nothing ever says so.
    """
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 2)
    suggestion = _suggestion(db, site, *articles)
    _always_fails(monkeypatch)

    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id)
    db.expire_all()
    assert db.get(Suggestion, suggestion.id).publish_attempts == 1
    assert _status(db, suggestion.id) == "approved"  # one bad night is not terminal

    with pytest.raises(RuntimeError, match="quarantined after 2 attempts"):
        publish_approved(site.id)
    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert stored.status == "failed"
    assert stored.publish_attempts == 2
    assert "WP stayed unavailable" in stored.publish_error


def test_quarantine_disables_the_remaining_rq_retry(
    db, site, articles, monkeypatch
):
    """A terminal row must not produce an empty retry that reports success."""
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 1)
    suggestion = _suggestion(db, site, *articles)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    rq_job = SimpleNamespace(retries_left=2)
    monkeypatch.setattr(job_service, "get_current_job", lambda: rq_job)
    _always_fails(monkeypatch)

    with pytest.raises(job_service.NonRetryableTaskError, match="quarantined"):
        publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    stored_run = db.get(JobRun, run.id)
    assert rq_job.retries_left == 0
    assert stored_run.status == "failed"
    assert stored_run.progress["failure_state"] == "terminal"
    assert _status(db, suggestion.id) == "failed"


def test_a_quarantined_suggestion_is_not_picked_up_again(db, site, articles, monkeypatch):
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 1)
    suggestion = _suggestion(db, site, *articles)
    _always_fails(monkeypatch)
    with pytest.raises(RuntimeError):
        publish_approved(site.id)
    assert _status(db, suggestion.id) == "failed"

    calls = []
    _stub_connector(monkeypatch, lambda s: calls.append(s.id))
    assert _core(publish_approved(site.id)) == {"applied": 0, "failed": 0, "skipped": 0}
    assert calls == []


def test_a_successful_publish_clears_the_failure_history(db, site, articles, monkeypatch):
    """The counter is for rows that never work, not for rows that had a bad night."""
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 5)
    suggestion = _suggestion(db, site, *articles)
    _always_fails(monkeypatch)
    with pytest.raises(RuntimeError):
        publish_approved(site.id)

    _stub_connector(monkeypatch, lambda _s: None)
    publish_approved(site.id)

    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert stored.status == "applied"
    assert stored.publish_attempts == 0
    assert stored.publish_error is None


# -- what was written (findings 12 and 14) ---------------------------------


def test_each_suggestion_records_what_the_connector_actually_did(
    db, site, articles, monkeypatch
):
    """"applied" says a write happened, not that a link is in the prose.

    The in-text share is the number that says whether paying for placement
    generation is worth it, and there was nowhere to read it from.
    """
    src, tgt = articles
    other_target = Article(
        site_id=site.id, url=f"{site.base_url}/tgt2", title="tgt2", content_text="c"
    )
    db.add(other_target)
    db.commit()
    in_text = _suggestion(db, site, src, tgt, score=0.95)
    appended = _suggestion(db, site, src, other_target, score=0.60)

    outcomes = iter(["inserted", "block"])
    monkeypatch.setattr(
        publication,
        "get_connector",
        lambda site: SimpleNamespace(
            apply_links=lambda suggestions, **_kwargs: [next(outcomes) for _ in suggestions]
        ),
    )
    result = publish_approved(site.id)

    assert result["inserted"] == 1
    assert result["block"] == 1
    assert result["already_present"] == 0
    db.expire_all()
    assert db.get(Suggestion, in_text.id).publish_outcome == "inserted"
    assert db.get(Suggestion, appended.id).publish_outcome == "block"


# -- preflight placement generation (findings 2 and 13) --------------------


@pytest.fixture
def preflight(monkeypatch):
    """Turn the preflight pass on, with a stubbed model. Returns the call log."""
    monkeypatch.setattr(settings, "publish_max_placement_calls_per_run", 10)
    monkeypatch.setattr(publication.openrouter, "is_configured", lambda: True)
    calls = []

    def generate(suggestion, taken_anchors=()):
        calls.append((suggestion.id, list(taken_anchors)))
        return placement_service.Placement(
            anchor_text=f"anchor-{suggestion.id}",
            placement_context="context",
            llm_model="stub",
            generated_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(publication.placement_service, "generate", generate)
    return calls


def test_bulk_approved_rows_get_a_placement_before_anything_is_written(
    db, site, articles, monkeypatch, preflight
):
    """Generation is lazy, so a bulk-approved row reaches publication with no anchor.

    That is the normal path — an editor approves in bulk and never opens a
    drawer — which meant almost every published link was the appended block and
    the in-text feature effectively never fired.
    """
    suggestion = _suggestion(db, site, *articles)
    anchors = []
    _stub_connector(monkeypatch, lambda s: anchors.append(s.anchor_text))

    publish_approved(site.id)

    assert preflight == [(suggestion.id, [])]
    assert anchors == [f"anchor-{suggestion.id}"]  # generated before the write, not after


def test_a_row_that_already_has_a_placement_is_not_regenerated(
    db, site, articles, monkeypatch, preflight
):
    suggestion = _suggestion(db, site, *articles)
    suggestion.anchor_text = "already chosen"
    suggestion.placement_generated_at = datetime.now(timezone.utc)
    db.commit()
    _stub_connector(monkeypatch, lambda _s: None)

    publish_approved(site.id)

    assert preflight == []


def test_each_call_is_told_which_anchors_its_siblings_took(
    db, site, articles, monkeypatch, preflight
):
    """One source article, generated one row at a time — the model cannot know."""
    src, tgt = articles
    other_target = Article(
        site_id=site.id, url=f"{site.base_url}/tgt2", title="tgt2", content_text="c"
    )
    db.add(other_target)
    db.commit()
    stronger = _suggestion(db, site, src, tgt, score=0.95)
    weaker = _suggestion(db, site, src, other_target, score=0.60)
    _stub_connector(monkeypatch, lambda _s: None)

    publish_approved(site.id)

    assert preflight == [
        (stronger.id, []),
        (weaker.id, [f"anchor-{stronger.id}"]),
    ]


def test_preflight_stops_at_the_call_budget(db, site, articles, monkeypatch, preflight):
    """A run over a large queue is a bill; the rest publish as the block."""
    monkeypatch.setattr(settings, "publish_max_placement_calls_per_run", 1)
    _suggestions_on_distinct_sources(db, site, articles[1], 3)
    _stub_connector(monkeypatch, lambda _s: None)

    result = publish_approved(site.id)

    assert len(preflight) == 1
    assert result["applied"] == 3  # the other two still publish


def test_a_model_that_is_down_does_not_fail_the_publication_run(
    db, site, articles, monkeypatch, preflight
):
    """An unreachable model is a reason to publish the block, not to fail."""
    monkeypatch.setattr(
        publication.placement_service,
        "generate",
        lambda _s, taken_anchors=(): (_ for _ in ()).throw(RuntimeError("openrouter down")),
    )
    _suggestion(db, site, *articles)
    anchors = []
    _stub_connector(monkeypatch, lambda s: anchors.append(s.anchor_text))

    assert _core(publish_approved(site.id)) == {"applied": 1, "failed": 0, "skipped": 0}
    assert anchors == [None]


def test_preflight_does_not_run_when_it_is_switched_off(db, site, articles, monkeypatch):
    """The suite's default, and the deployment default for anyone without a key."""
    monkeypatch.setattr(publication.openrouter, "is_configured", lambda: True)
    monkeypatch.setattr(
        publication.placement_service,
        "generate",
        lambda *_args, **_kwargs: pytest.fail("preflight ran with a zero budget"),
    )
    _suggestion(db, site, *articles)
    _stub_connector(monkeypatch, lambda _s: None)

    assert publish_approved(site.id)["applied"] == 1


# -- dry run (finding 14) --------------------------------------------------


def test_dry_run_previews_what_publication_would_write(client, db, site, articles, monkeypatch):
    """There was no way to see this before it happened, only after."""
    suggestion = _suggestion(db, site, *articles)
    asked = []
    prepared = []

    monkeypatch.setattr(
        "app.api.routes.publish.generate_missing_placements",
        lambda groups, job_run_id: prepared.append((groups, job_run_id)),
    )

    def preview_links(suggestions):
        asked.append([row.id for row in suggestions])
        return LinkPreview(
            original_content="<p>Before</p>",
            updated_content='<p>Before <a href="https://example.com/tgt">target</a></p>',
            outcomes=["inserted"] * len(suggestions),
        )

    monkeypatch.setattr(
        "app.api.routes.publish.get_connector",
        lambda site: SimpleNamespace(preview_links=preview_links),
    )

    body = client.get(f"/api/v1/publish/{site.id}/dry-run").json()

    assert prepared == [([(articles[0].id, [suggestion.id])], None)]
    assert asked == [[suggestion.id]]
    assert body["approved"] == 1
    assert body["previewed"] == 1
    assert body["inserted"] == 1
    assert body["placements_missing"] == 1
    assert body["planned"] == [
        {
            "suggestion_id": suggestion.id,
            "source_article_id": articles[0].id,
            "source_url": articles[0].url,
            "target_url": articles[1].url,
            "outcome": "inserted",
            "anchor_text": None,
        }
    ]
    assert body["articles"] == [
        {
            "source_article_id": articles[0].id,
            "source_url": articles[0].url,
            "original_html": "<p>Before</p>",
            "updated_html": '<p>Before <a href="https://example.com/tgt">target</a></p>',
            "links": body["planned"],
        }
    ]
    assert body["errors"] == []
    assert body["truncated"] is False
    assert _status(db, suggestion.id) == "approved"  # nothing claimed


def test_dry_run_is_bounded_and_survives_an_unreachable_post(
    client, db, site, articles, monkeypatch
):
    """One dead post must not lose the preview of the others, and 5000 approved
    suggestions must not become 5000 synchronous requests inside one HTTP call."""
    _suggestions_on_distinct_sources(db, site, articles[1], 3)
    seen = []

    def preview_links(suggestions):
        seen.append(suggestions[0].source_article_id)
        if len(seen) == 1:
            raise RuntimeError("post is gone")
        return LinkPreview("<p>Before</p>", "<p>Before</p><ul></ul>", ["block"])

    monkeypatch.setattr(
        "app.api.routes.publish.get_connector",
        lambda site: SimpleNamespace(preview_links=preview_links),
    )

    body = client.get(f"/api/v1/publish/{site.id}/dry-run", params={"max_articles": 2}).json()

    assert len(seen) == 2  # bounded
    assert body["approved"] == 3
    assert body["previewed"] == 1  # the reachable one of the two
    assert body["block"] == 1
    assert body["truncated"] is True
    assert len(body["errors"]) == 1
    assert body["errors"][0]["message"] == "post is gone"


def test_dry_run_refuses_a_content_pool_source(client, db, site):
    site.platform = "pool"
    db.commit()
    assert client.get(f"/api/v1/publish/{site.id}/dry-run").status_code == 409


def test_review_endpoint_rejects_applied_and_applying(client, db, site, articles):
    for status in ("applied", "applying"):
        suggestion = _suggestion(db, site, *articles, status=status)
        resp = client.put(f"/api/v1/suggestions/{suggestion.id}", json={"status": "rejected"})
        assert resp.status_code == 409, resp.text
        assert _status(db, suggestion.id) == status


def test_review_endpoint_still_allows_changing_a_decision(client, db, site, articles):
    suggestion = _suggestion(db, site, *articles, status="rejected")
    resp = client.put(f"/api/v1/suggestions/{suggestion.id}", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert _status(db, suggestion.id) == "approved"


# -- accounting under partial and failed writes ----------------------------


def _commit_fails_after(monkeypatch, allowed: int):
    """Break the publication session's commit once it has allowed `allowed` of them.

    `_publish_approved` opens its work session first and its progress session
    second, so wrapping the first one puts the failure exactly on the commit that
    is supposed to make a write durable.
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


def test_a_commit_that_fails_does_not_count_its_links_as_applied(
    db, site, articles, monkeypatch
):
    """The claim rolls back to 'approved', so the counters have to roll back too.

    Counting a link the moment the connector returns means a failed commit
    reports the same suggestion as applied *and* failed, and the retry then
    resumes from an 'applied' total that includes a link nobody wrote.
    """
    suggestion = _suggestion(db, site, *articles)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    _stub_connector(monkeypatch, lambda _s: None)
    _commit_fails_after(monkeypatch, allowed=1)  # the batch-open commit survives

    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    progress = db.get(JobRun, run.id).progress
    assert (progress["applied"], progress["failed"]) == (0, 1)
    assert _status(db, suggestion.id) == "approved"
    db.expire_all()
    assert db.get(Suggestion, suggestion.id).publish_attempts == 0


def test_failure_success_failure_keeps_committed_outcome_counts(
    db, site, articles, monkeypatch
):
    """A later durable failure must not overwrite a successful middle group."""
    suggestions = _suggestions_on_distinct_sources(db, site, articles[1], 3)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    calls = []

    def apply_links(rows, *, dry_run=False):
        calls.append(rows[0].id)
        if len(calls) in (1, 3):
            raise RuntimeError(f"failure {len(calls)}")
        return ["inserted"]

    monkeypatch.setattr(
        publication,
        "get_connector",
        lambda site: SimpleNamespace(apply_links=apply_links),
    )

    with pytest.raises(RuntimeError, match="2 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    progress = db.get(JobRun, run.id).progress
    assert calls == [row.id for row in suggestions]
    assert (progress["applied"], progress["failed"], progress["inserted"]) == (1, 2, 1)


def test_a_partly_claimed_article_counts_each_suggestion_once(
    db, site, articles, monkeypatch
):
    """Skipped and failed are different rows; together they are still the group.

    One source article can hold several suggestions. Whatever is no longer
    claimable is counted as skipped, and counting the whole group again as failed
    reports more outcomes than the article ever had suggestions.
    """
    src, tgt = articles
    gone = Article(
        site_id=site.id,
        url=f"{site.base_url}/gone",
        title="gone",
        content_text="c",
        is_active=False,
    )
    db.add(gone)
    db.commit()
    _suggestion(db, site, src, tgt)
    _suggestion(db, site, src, gone)  # same source, target no longer active
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    _always_fails(monkeypatch)

    with pytest.raises(RuntimeError, match="1 publication.*failed"):
        publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    progress = db.get(JobRun, run.id).progress
    assert (progress["skipped"], progress["failed"], progress["total"]) == (1, 1, 2)


def test_the_preflight_counters_survive_the_run_that_follows_them(
    db, site, articles, monkeypatch, preflight
):
    """The preflight commits through its own session; the run must not undo that.

    Sessions are expire_on_commit=False, so the publication session still holds
    the JobRun as it was before the preflight. Merging the next progress update
    onto that stale copy writes the whole column back without the placement
    counters, and the one number that says whether paying for generation is
    worth it is the one that disappears.
    """
    _suggestion(db, site, *articles)
    run = JobRun(site_id=site.id, kind="publication")
    db.add(run)
    db.commit()
    _stub_connector(monkeypatch, lambda _s: None)

    publish_approved(site.id, job_run_id=run.id)

    db.expire_all()
    progress = db.get(JobRun, run.id).progress
    assert (progress["placement_calls"], progress["placements_generated"]) == (1, 1)
    assert progress["applied"] == 1  # and the run's own counters are still there


def test_re_approving_a_quarantined_suggestion_gives_it_a_real_second_chance(
    client, db, site, articles, monkeypatch
):
    """Undo-then-approve has to clear the failure count, not just the status.

    The quarantine triggers at `>= limit`, so a revived row still sitting at the
    limit is quarantined again by its very next attempt. That makes the editor's
    decision look like it worked while changing nothing.
    """
    monkeypatch.setattr(settings, "publish_max_suggestion_attempts", 2)
    suggestion = _suggestion(db, site, *articles)
    _always_fails(monkeypatch)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            publish_approved(site.id)
    assert _status(db, suggestion.id) == "failed"

    for status in ("pending", "approved"):  # what Undo then Accept sends
        assert (
            client.put(
                f"/api/v1/suggestions/{suggestion.id}", json={"status": status}
            ).status_code
            == 200
        )

    with pytest.raises(RuntimeError):
        publish_approved(site.id)

    db.expire_all()
    revived = db.get(Suggestion, suggestion.id)
    assert (revived.status, revived.publish_attempts) == ("approved", 1)
