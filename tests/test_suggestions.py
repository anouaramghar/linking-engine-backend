"""Baseline cosine + review lifecycle — hand-crafted embeddings, no torch needed."""

import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Lock

import pytest
from sqlalchemy import event, select, update

import app.services.suggestion_service as suggestion_service
from app.config import settings
from app.db import engine
from app.models import Article, Embedding, IngestionRun, InternalLink, Suggestion
from app.models.article import EMBEDDING_DIM
from app.services.ingestion_service import _reconcile_snapshot
from app.services.suggestion_service import generate_suggestions


def _vec(direction: int, weight: float = 1.0) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[direction] = weight
    return v


@pytest.fixture(autouse=True)
def valid_dimension_probe(monkeypatch):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vec(0) for _text in texts],
    )


def _mix(a: int, b: int, wa: float, wb: float) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[a], v[b] = wa, wb
    return v


def _make_articles(db, site, vectors):
    articles = []
    for i, vector in enumerate(vectors):
        title = f"art {i}"
        text = f"content {i}"
        art = Article(
            site_id=site.id,
            url=f"{site.base_url}/art-{i}",
            title=title,
            content_text=text,
        )
        db.add(art)
        db.flush()
        db.add(
            Embedding(
                article_id=art.id,
                model=settings.embedding_model,
                vector=vector,
                content_fingerprint=hashlib.sha256(f"{title}\n{text}".encode()).hexdigest(),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            )
        )
        articles.append(art)
    db.commit()
    return articles


def test_baseline_suggestions(db, site):
    # a0 and a1 nearly identical, a2 close to both, a3 orthogonal (off-topic)
    a = _make_articles(db, site, [
        _vec(0), _mix(0, 1, 0.98, 0.2), _mix(0, 1, 0.7, 0.7), _vec(2),
    ])
    # a0 -> a1 already linked: must NOT be suggested again
    db.add(InternalLink(source_article_id=a[0].id, target_article_id=a[1].id))
    db.commit()

    result = generate_suggestions(site.id)
    assert result["articles_encoded"] == 0  # all pre-embedded, no model download

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    by_source = {}
    for s in suggestions:
        by_source.setdefault(s.source_article_id, []).append(s)
        assert s.method == "baseline_cosine"
        assert s.status == "pending"
        assert 0.0 <= s.score <= 1.0001

    # a0: a1 excluded (existing link) -> targets a2 and a3 only
    a0_targets = {s.target_article_id for s in by_source[a[0].id]}
    assert a[1].id not in a0_targets
    assert a[2].id in a0_targets

    # best target of a1 must be a0 (nearly identical vectors)
    best = max(by_source[a[1].id], key=lambda s: s.score)
    assert best.target_article_id == a[0].id
    assert best.score > 0.9

    # max 5 per article (A4)
    assert all(len(v) <= settings.max_suggestions_per_article for v in by_source.values())

    # re-run: no duplicates
    generate_suggestions(site.id)
    total = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert len(total) == len(suggestions)


def test_dimension_mismatch_fails_before_article_embedding(db, site, monkeypatch):
    produced_size = EMBEDDING_DIM - 1
    probe_calls = []

    def wrong_dimension(texts):
        probe_calls.append(texts)
        return [[0.0] * produced_size]

    monkeypatch.setattr("app.ml.embeddings.encode", wrong_dimension)
    monkeypatch.setattr(
        suggestion_service,
        "_embed_missing",
        lambda *_args: pytest.fail("article scan must not start after a failed probe"),
    )

    with pytest.raises(ValueError, match=rf"{produced_size}.*{EMBEDDING_DIM}"):
        generate_suggestions(site.id)

    assert probe_calls == [[suggestion_service._DIMENSION_PROBE_INPUT]]


def test_analysis_uses_only_current_articles_and_links(db, site):
    source, inactive, target = _make_articles(
        db,
        site,
        [_vec(0), _mix(0, 1, 0.99, 0.1), _mix(0, 1, 0.98, 0.2)],
    )
    inactive.is_active = False
    expired_link = InternalLink(
        source_article_id=source.id,
        target_article_id=target.id,
    )
    expired_link.is_active = False
    db.add(expired_link)
    db.commit()

    generate_suggestions(site.id)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert suggestions
    assert inactive.id not in {suggestion.source_article_id for suggestion in suggestions}
    assert inactive.id not in {suggestion.target_article_id for suggestion in suggestions}
    assert target.id in {
        suggestion.target_article_id
        for suggestion in suggestions
        if suggestion.source_article_id == source.id
    }


def test_reanalysis_respects_total_suggestion_cap(db, site):
    _make_articles(db, site, [_vec(0) for _ in range(7)])

    first = generate_suggestions(site.id)
    second = generate_suggestions(site.id)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    counts = Counter(suggestion.source_article_id for suggestion in suggestions)
    assert first == {"articles_encoded": 0, "suggestions_created": 35}
    assert second == {"articles_encoded": 0, "suggestions_created": 0}
    assert set(counts.values()) == {settings.max_suggestions_per_article}


def test_rejected_and_applied_suggestions_free_active_quota(db, site):
    articles = _make_articles(
        db,
        site,
        [_vec(0) for _ in range(settings.max_suggestions_per_article + 3)],
    )
    source_id = articles[0].id
    generate_suggestions(site.id)

    original = db.scalars(
        select(Suggestion)
        .where(Suggestion.source_article_id == source_id)
        .order_by(Suggestion.id)
    ).all()
    assert len(original) == settings.max_suggestions_per_article
    rejected, applied = original[:2]
    rejected.status = "rejected"
    applied.status = "applied"
    decided_target_ids = {rejected.target_article_id, applied.target_article_id}
    original_ids = {suggestion.id for suggestion in original}
    db.commit()

    result = generate_suggestions(site.id)

    suggestions = db.scalars(
        select(Suggestion)
        .where(Suggestion.source_article_id == source_id)
        .order_by(Suggestion.id)
    ).all()
    active = [
        suggestion
        for suggestion in suggestions
        if suggestion.status in {"pending", "approved", "applying"}
    ]
    replacements = [suggestion for suggestion in suggestions if suggestion.id not in original_ids]
    assert result == {"articles_encoded": 0, "suggestions_created": 2}
    assert len(active) == settings.max_suggestions_per_article
    assert len(replacements) == 2
    assert decided_target_ids.isdisjoint(
        {suggestion.target_article_id for suggestion in replacements}
    )


def test_expired_suggestion_frees_source_quota(db, site):
    articles = _make_articles(
        db,
        site,
        [_vec(0) for _ in range(settings.max_suggestions_per_article + 2)],
    )
    generate_suggestions(site.id)
    source = articles[0]
    original = db.scalars(
        select(Suggestion)
        .where(Suggestion.source_article_id == source.id)
        .order_by(Suggestion.id)
    ).all()
    expired = original[0]
    inactive_target = db.get(Article, expired.target_article_id)
    original_ids = {suggestion.id for suggestion in original}

    run = IngestionRun(site_id=site.id)
    db.add(run)
    db.flush()
    for article in articles:
        if article.id != inactive_target.id:
            article.last_seen_run_id = run.id
    db.flush()
    _reconcile_snapshot(db, site.id, run.id)
    db.commit()

    assert db.get(Suggestion, expired.id).status == "expired"
    generate_suggestions(site.id)

    source_suggestions = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source.id)
    ).all()
    active = [
        suggestion
        for suggestion in source_suggestions
        if suggestion.status in {"pending", "approved", "applying"}
    ]
    replacements = [
        suggestion for suggestion in source_suggestions if suggestion.id not in original_ids
    ]
    assert len(active) == settings.max_suggestions_per_article
    assert len(replacements) == 1


def test_expired_pair_is_suggested_again_after_article_reactivation(db, site):
    source, target = _make_articles(db, site, [_vec(0), _vec(0)])
    generate_suggestions(site.id)
    original = db.scalar(
        select(Suggestion).where(
            Suggestion.source_article_id == source.id,
            Suggestion.target_article_id == target.id,
        )
    )

    deactivate_run = IngestionRun(site_id=site.id)
    db.add(deactivate_run)
    db.flush()
    source.last_seen_run_id = deactivate_run.id
    db.flush()
    _reconcile_snapshot(db, site.id, deactivate_run.id)
    db.commit()
    assert db.get(Suggestion, original.id).status == "expired"
    assert target.is_active is False

    reactivate_run = IngestionRun(site_id=site.id)
    db.add(reactivate_run)
    db.flush()
    source.last_seen_run_id = reactivate_run.id
    target.last_seen_run_id = reactivate_run.id
    db.flush()
    _reconcile_snapshot(db, site.id, reactivate_run.id)
    db.commit()
    assert target.is_active is True

    generate_suggestions(site.id)

    matching = db.scalars(
        select(Suggestion)
        .where(
            Suggestion.source_article_id == source.id,
            Suggestion.target_article_id == target.id,
        )
        .order_by(Suggestion.id)
    ).all()
    assert [suggestion.status for suggestion in matching] == ["expired", "pending"]
    assert matching[0].id == original.id
    assert matching[1].id != original.id


def test_rejected_source_target_pair_is_not_recreated(db, site):
    source, target = _make_articles(db, site, [_vec(0), _vec(0)])
    generate_suggestions(site.id)
    suggestion = db.scalar(
        select(Suggestion).where(
            Suggestion.source_article_id == source.id,
            Suggestion.target_article_id == target.id,
        )
    )
    suggestion.status = "rejected"
    db.commit()

    result = generate_suggestions(site.id)

    matching = db.scalars(
        select(Suggestion).where(
            Suggestion.source_article_id == source.id,
            Suggestion.target_article_id == target.id,
        )
    ).all()
    assert result == {"articles_encoded": 0, "suggestions_created": 0}
    assert len(matching) == 1
    assert matching[0].status == "rejected"


def test_concurrent_analysis_respects_total_suggestion_cap(db, site):
    _make_articles(db, site, [_vec(0) for _ in range(7)])
    worker_entries = Barrier(2)
    candidate_snapshots = Barrier(2)
    listener_lock = Lock()
    paused_queries = 0

    def synchronize_first_candidate_queries(
        conn, cursor, statement, parameters, context, executemany
    ):
        nonlocal paused_queries
        if "SELECT a2.id AS target_id" not in statement:
            return
        with listener_lock:
            if paused_queries >= 2:
                return
            paused_queries += 1
        try:
            candidate_snapshots.wait(timeout=2)
        except BrokenBarrierError:
            pass

    event.listen(engine, "after_cursor_execute", synchronize_first_candidate_queries)
    try:
        def analyze(site_id):
            worker_entries.wait(timeout=2)
            return generate_suggestions(site_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(analyze, [site.id, site.id]))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_first_candidate_queries)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    counts = Counter(suggestion.source_article_id for suggestion in suggestions)
    assert sum(result["suggestions_created"] for result in results) == 35
    assert len(suggestions) == 35
    assert set(counts.values()) == {settings.max_suggestions_per_article}


def test_review_lifecycle(client, db, site):
    _make_articles(db, site, [_vec(0), _mix(0, 1, 0.9, 0.3)])
    generate_suggestions(site.id)
    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    first, second = suggestions[0], suggestions[1]

    # single review
    resp = client.put(f"/api/v1/suggestions/{first.id}", json={"status": "approved"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["source_article"]["title"].startswith("art")

    # bulk review
    resp = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [second.id], "status": "rejected"},
    )
    assert resp.json() == {"reviewed": 1, "status": "rejected"}

    # 'applied' can never be set via the API
    resp = client.put(f"/api/v1/suggestions/{first.id}", json={"status": "applied"})
    assert resp.status_code == 422

    # list with filter
    listed = client.get(f"/api/v1/suggestions/{site.id}", params={"status": "approved"}).json()
    assert [s["id"] for s in listed] == [first.id]

    # publication status counts
    status = client.get(f"/api/v1/publish/{site.id}/status").json()
    assert status == {"applied": 0, "awaiting_publication": 1}


def test_review_can_be_undone(client, db, site):
    """An editor can return a reviewed suggestion to the pending queue."""
    _make_articles(db, site, [_vec(0), _mix(0, 1, 0.9, 0.3)])
    generate_suggestions(site.id)
    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    first, second = suggestions[0], suggestions[1]

    client.put(f"/api/v1/suggestions/{first.id}", json={"status": "approved"})
    resp = client.put(f"/api/v1/suggestions/{first.id}", json={"status": "pending"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    resp = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [second.id], "status": "rejected"},
    )
    assert resp.json() == {"reviewed": 1, "status": "rejected"}
    resp = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [second.id], "status": "pending"},
    )
    assert resp.json() == {"reviewed": 1, "status": "pending"}

    # undo restores the unreviewed state, timestamp included
    db.expire_all()
    restored = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert {s.status for s in restored} == {"pending"}
    assert all(s.reviewed_at is None for s in restored)

    # a published suggestion still cannot be walked back
    db.execute(
        update(Suggestion).where(Suggestion.id == first.id).values(status="applied")
    )
    db.commit()
    resp = client.put(f"/api/v1/suggestions/{first.id}", json={"status": "pending"})
    assert resp.status_code == 409


def test_list_endpoints_reject_an_unbounded_limit(client, site):
    """A single request must not be able to ask for the whole table."""
    assert client.get(f"/api/v1/suggestions/{site.id}", params={"limit": 10_000_000}).status_code == 422
    assert client.get("/api/v1/sites", params={"limit": 10_000_000}).status_code == 422
    assert client.get(f"/api/v1/suggestions/{site.id}", params={"offset": -1}).status_code == 422
    # the page size the dashboard actually uses stays valid
    assert client.get(f"/api/v1/suggestions/{site.id}", params={"limit": 1000}).status_code == 200
