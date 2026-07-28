"""Safe publication (Phase 0, finding 5): claim, per-article serialization, reject
races, and retry semantics. Connector is stubbed; locking runs against real PostgreSQL."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import SessionLocal
from app.models import Alert, Article, JobRun, Suggestion
from app.services import job_service
from app.tasks import publication
from app.tasks.publication import publish_approved


@pytest.fixture
def articles(db, site):
    src = Article(site_id=site.id, url=f"{site.base_url}/src", title="src", content_text="a")
    tgt = Article(site_id=site.id, url=f"{site.base_url}/tgt", title="tgt", content_text="b")
    db.add_all([src, tgt])
    db.commit()
    return src, tgt  # cascade-deleted with the site


def _suggestion(db, site, src, tgt, status="approved"):
    s = Suggestion(
        site_id=site.id,
        source_article_id=src.id,
        target_article_id=tgt.id,
        method="baseline_cosine",
        score=0.9,
        status=status,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _stub_connector(monkeypatch, apply_link):
    monkeypatch.setattr(
        publication, "get_connector", lambda site: SimpleNamespace(apply_link=apply_link)
    )


def _status(db, suggestion_id):
    db.expire_all()
    return db.get(Suggestion, suggestion_id).status


def test_publish_applies_approved_suggestion(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    calls = []
    _stub_connector(monkeypatch, lambda s: calls.append(s.id))

    result = publish_approved(site.id)

    assert result == {"applied": 1, "failed": 0, "skipped": 0}
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

    assert result == {"applied": 0, "failed": 0, "skipped": 1}
    assert calls == []
    assert _status(db, suggestion.id) == "approved"


def test_double_publish_applies_exactly_once(db, site, articles, monkeypatch):
    suggestion = _suggestion(db, site, *articles)
    calls = []
    _stub_connector(monkeypatch, lambda s: calls.append(s.id))

    first = publish_approved(site.id)
    second = publish_approved(site.id)

    assert first["applied"] == 1
    assert second == {"applied": 0, "failed": 0, "skipped": 0}
    assert calls == [suggestion.id]


def test_suggestion_rejected_after_batch_select_is_skipped(db, site, articles, monkeypatch):
    """The claim (approved -> applying) re-checks status; a reject that lands between
    the batch select and the claim must win."""
    src, tgt = articles
    first = _suggestion(db, site, src, tgt)
    second = _suggestion(db, site, tgt, src)
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

    assert result == {"applied": 1, "failed": 0, "skipped": 1}
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
    suggestions = [_suggestion(db, site, *articles) for _ in range(10)]
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
    assert result == {"applied": 10, "failed": 0, "skipped": 0}
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
    }


def test_retry_grows_total_for_suggestions_approved_between_attempts(
    db, site, articles, monkeypatch
):
    suggestions = [_suggestion(db, site, *articles) for _ in range(10)]
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

    for _ in range(5):
        _suggestion(db, site, *articles)
    _stub_connector(monkeypatch, lambda _suggestion: None)
    result = publish_approved(site.id, job_run_id=run.id)

    assert result == {"applied": 15, "failed": 0, "skipped": 0}
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

    assert result == {"applied": 0, "failed": 0, "skipped": 1}
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
    }
    alert = db.scalar(select(Alert).where(Alert.site_id == site.id))
    assert alert.kind == "job_failed"
    assert alert.subject == "LinkMesh publication job failed"


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
