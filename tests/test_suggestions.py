"""Baseline cosine + review lifecycle — hand-crafted embeddings, no torch needed."""

import hashlib
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Lock

import pytest
from sqlalchemy import event, select, update

import app.services.suggestion_service as suggestion_service
from app.api.pagination import MAX_PAGE_SIZE
from app.config import settings
from app.schemas.suggestion import MAX_BULK_REVIEW
from app.db import engine
from app.models import (
    Article,
    Embedding,
    ExternalLinkPolicy,
    IngestionRun,
    InternalLink,
    Site,
    Suggestion,
)
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


@pytest.fixture
def source_with_pool_targets(db):
    """One customer source plus one-way external targets for quota tests.

    Internal pairs now deliberately have only one proposed direction, so a
    content pool isolates per-source quota behavior without a reverse proposal
    from each target consuming the replacement candidates.

    The pool is approved because these tests are about quota accounting, not
    about the approval gate: an unapproved pool contributes no candidates at
    all, which would empty the queue these tests measure rather than exercise
    it. `test_content_pool.py` owns the gate itself.
    """
    pool_ids = []

    def make(site, target_count):
        source = _make_articles(db, site, [_vec(0)])[0]
        pool = Site(
            name="Test pool",
            base_url=f"https://pool-{uuid.uuid4().hex[:8]}.example",
            platform="pool",
            pool_source_approved=True,
        )
        db.add(pool)
        db.add(ExternalLinkPolicy(site_id=site.id, external_links_enabled=True))
        db.commit()
        pool_ids.append(pool.id)
        targets = _make_articles(db, pool, [_vec(0) for _ in range(target_count)])
        return source, pool, targets

    yield make

    db.rollback()
    for pool_id in pool_ids:
        pool = db.get(Site, pool_id)
        if pool is not None:
            db.delete(pool)
    db.commit()


def test_baseline_suggestions(db, site):
    # a0 and a1 nearly identical, a2 close to both, a3 orthogonal (off-topic)
    a = _make_articles(
        db,
        site,
        [
            _vec(0),
            _mix(0, 1, 0.98, 0.2),
            _mix(0, 1, 0.7, 0.7),
            _vec(2),
        ],
    )
    # a0 -> a1 already linked: must NOT be suggested again
    db.add(InternalLink(source_article_id=a[0].id, target_article_id=a[1].id))
    db.commit()

    result = generate_suggestions(site.id, ranking_mode_override="shadow")
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
    assert all(len(v) <= settings.hybrid_max_suggestions_per_article for v in by_source.values())

    # re-run: no duplicates
    generate_suggestions(site.id, ranking_mode_override="shadow")
    total = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert len(total) == len(suggestions)


def test_generation_rejects_pairs_below_the_minimum_score(db, site, monkeypatch):
    monkeypatch.setattr(settings, "suggestion_min_score", 0.50)
    source, strong, weak = _make_articles(
        db,
        site,
        [
            _vec(0),
            _mix(0, 1, 0.60, 0.80),
            _mix(0, 1, 0.49, 0.872),
        ],
    )

    generate_suggestions(site.id, ranking_mode_override="baseline")

    targets = set(
        db.scalars(
            select(Suggestion.target_article_id).where(Suggestion.source_article_id == source.id)
        )
    )
    assert strong.id in targets
    assert weak.id not in targets


def test_generation_creates_only_one_direction_for_each_pair(db, site):
    _make_articles(db, site, [_vec(0) for _ in range(5)])

    generate_suggestions(site.id, ranking_mode_override="baseline")

    pairs = {
        (row.source_article_id, row.target_article_id)
        for row in db.scalars(select(Suggestion).where(Suggestion.site_id == site.id))
    }
    assert all((target, source) not in pairs for source, target in pairs)


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

    first = generate_suggestions(site.id, ranking_mode_override="shadow")
    second = generate_suggestions(site.id, ranking_mode_override="shadow")

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    counts = Counter(suggestion.source_article_id for suggestion in suggestions)
    assert 0 < first["suggestions_created"] <= 21
    assert len(suggestions) == first["suggestions_created"] + second["suggestions_created"]
    assert max(counts.values()) <= settings.hybrid_max_suggestions_per_article


def test_rejected_and_applied_suggestions_free_active_quota(db, site, source_with_pool_targets):
    source, _pool, _targets = source_with_pool_targets(
        site, settings.hybrid_max_suggestions_per_article + 3
    )
    source_id = source.id
    generate_suggestions(site.id, ranking_mode_override="baseline")

    original = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source_id).order_by(Suggestion.id)
    ).all()
    assert len(original) == settings.hybrid_max_suggestions_per_article
    rejected, applied = original[:2]
    rejected.status = "rejected"
    applied.status = "applied"
    decided_target_ids = {rejected.target_article_id, applied.target_article_id}
    original_ids = {suggestion.id for suggestion in original}
    db.commit()

    result = generate_suggestions(site.id, ranking_mode_override="baseline")

    suggestions = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source_id).order_by(Suggestion.id)
    ).all()
    active = [
        suggestion
        for suggestion in suggestions
        if suggestion.status in {"pending", "approved", "applying"}
    ]
    replacements = [suggestion for suggestion in suggestions if suggestion.id not in original_ids]
    assert result["suggestions_created"] == 2
    assert len(active) == settings.hybrid_max_suggestions_per_article
    assert len(replacements) == 2
    assert decided_target_ids.isdisjoint(
        {suggestion.target_article_id for suggestion in replacements}
    )


def test_applied_suggestions_still_count_against_the_lifetime_cap(
    db, site, monkeypatch, source_with_pool_targets
):
    """Freeing the active slot must not let successive runs link a source for ever."""
    monkeypatch.setattr(settings, "hybrid_max_lifetime_links_per_article", 4)
    source, _pool, _targets = source_with_pool_targets(site, 8)
    source_id = source.id
    generate_suggestions(site.id, ranking_mode_override="baseline")

    # Publish the whole first batch: the queue empties, the article does not.
    db.execute(
        update(Suggestion).where(Suggestion.source_article_id == source_id).values(status="applied")
    )
    db.commit()

    generate_suggestions(site.id, ranking_mode_override="baseline")
    generate_suggestions(site.id, ranking_mode_override="baseline")

    counts = Counter(
        suggestion.status
        for suggestion in db.scalars(
            select(Suggestion).where(Suggestion.source_article_id == source_id)
        )
    )
    assert counts["applied"] == settings.hybrid_max_suggestions_per_article
    assert sum(counts.values()) == settings.hybrid_max_lifetime_links_per_article


def test_expired_suggestion_frees_source_quota(db, site, source_with_pool_targets):
    source, pool, targets = source_with_pool_targets(
        site, settings.hybrid_max_suggestions_per_article + 2
    )
    generate_suggestions(site.id, ranking_mode_override="baseline")
    original = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source.id).order_by(Suggestion.id)
    ).all()
    expired = original[0]
    inactive_target = db.get(Article, expired.target_article_id)
    original_ids = {suggestion.id for suggestion in original}

    run = IngestionRun(site_id=pool.id)
    db.add(run)
    db.flush()
    for article in targets:
        if article.id != inactive_target.id:
            article.last_seen_run_id = run.id
    db.flush()
    _reconcile_snapshot(db, pool.id, run.id)
    db.commit()

    assert db.get(Suggestion, expired.id).status == "expired"
    generate_suggestions(site.id, ranking_mode_override="baseline")

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
    assert len(active) == settings.hybrid_max_suggestions_per_article
    assert len(replacements) == 1


def test_expired_pair_is_suggested_again_after_article_reactivation(db, site):
    source, target = _make_articles(db, site, [_vec(0), _vec(0)])
    generate_suggestions(site.id, ranking_mode_override="shadow")
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

    generate_suggestions(site.id, ranking_mode_override="shadow")

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
    _make_articles(db, site, [_vec(0), _vec(0)])
    generate_suggestions(site.id, ranking_mode_override="shadow")
    suggestion = db.scalar(select(Suggestion).where(Suggestion.site_id == site.id))
    source_id = suggestion.source_article_id
    target_id = suggestion.target_article_id
    suggestion.status = "rejected"
    db.commit()

    result = generate_suggestions(site.id, ranking_mode_override="shadow")

    matching = db.scalars(
        select(Suggestion).where(
            Suggestion.source_article_id == source_id,
            Suggestion.target_article_id == target_id,
        )
    ).all()
    # The opposite direction remains a separate editorial decision and may be
    # proposed once the rejected direction is no longer active.
    assert result["suggestions_created"] == 1
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
            return generate_suggestions(site_id, ranking_mode_override="shadow")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(analyze, [site.id, site.id]))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_first_candidate_queries)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    counts = Counter(suggestion.source_article_id for suggestion in suggestions)
    assert sum(result["suggestions_created"] for result in results) == 21
    assert len(suggestions) == 21
    assert set(counts.values()) == {settings.hybrid_max_suggestions_per_article}


def test_review_lifecycle(client, db, site):
    _make_articles(db, site, [_vec(0), _mix(0, 1, 0.9, 0.3), _mix(0, 1, 0.8, 0.6)])
    generate_suggestions(site.id, ranking_mode_override="shadow")
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
    assert resp.json() == {"reviewed": [second.id], "skipped": [], "status": "rejected"}

    # 'applied' can never be set via the API
    resp = client.put(f"/api/v1/suggestions/{first.id}", json={"status": "applied"})
    assert resp.status_code == 422

    # list with filter
    listed = client.get(f"/api/v1/suggestions/{site.id}", params={"status": "approved"}).json()
    assert [s["id"] for s in listed] == [first.id]

    # publication status counts — a selected row is not yet a publishable plan
    status = client.get(f"/api/v1/publish/{site.id}/status").json()
    assert status == {"applied": 0, "selected_suggestions": 1, "approved_plans": 0}


def test_review_can_be_undone(client, db, site):
    """An editor can return a reviewed suggestion to the pending queue."""
    _make_articles(db, site, [_vec(0), _mix(0, 1, 0.9, 0.3), _mix(0, 1, 0.8, 0.6)])
    generate_suggestions(site.id, ranking_mode_override="shadow")
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
    assert resp.json() == {"reviewed": [second.id], "skipped": [], "status": "rejected"}
    resp = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [second.id], "status": "pending"},
    )
    assert resp.json() == {"reviewed": [second.id], "skipped": [], "status": "pending"}

    # undo restores the unreviewed state, timestamp included
    db.expire_all()
    restored = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert {s.status for s in restored} == {"pending"}
    assert all(s.reviewed_at is None for s in restored)

    # a published suggestion still cannot be walked back
    db.execute(update(Suggestion).where(Suggestion.id == first.id).values(status="applied"))
    db.commit()
    resp = client.put(f"/api/v1/suggestions/{first.id}", json={"status": "pending"})
    assert resp.status_code == 409


def test_bulk_review_applies_the_rows_it_can_and_reports_the_rest(client, db, site):
    """One row the worker already claimed must not discard the other decisions.

    Undo races the publication worker by design — the editor can hit Undo while
    a publish is picking the batch up — so the batch reports what it skipped
    instead of failing whole.
    """
    _make_articles(db, site, [_vec(0), _mix(0, 1, 0.9, 0.3), _mix(0, 1, 0.8, 0.6)])
    generate_suggestions(site.id, ranking_mode_override="shadow")
    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    first, second = suggestions[0], suggestions[1]

    client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [first.id, second.id], "status": "approved"},
    )
    # the worker claims one of them mid-flight
    db.execute(update(Suggestion).where(Suggestion.id == first.id).values(status="applied"))
    db.commit()

    resp = client.post(
        "/api/v1/suggestions/bulk-review",
        json={"suggestion_ids": [first.id, second.id], "status": "pending"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "reviewed": [second.id],
        "skipped": [first.id],
        "status": "pending",
    }

    db.expire_all()
    assert db.get(Suggestion, first.id).status == "applied"
    assert db.get(Suggestion, second.id).status == "pending"


def test_bulk_review_round_trips_do_not_scale_with_the_batch(client, db, site):
    """A batch of any size costs a fixed number of statements.

    Both halves have been one-per-suggestion at some point: the reviews used to
    be a loop of single-row UPDATEs, and reviewing expires each row, so reading
    any attribute afterwards silently reloads it. At the 1000-row bound either
    one is 1000 round trips inside a single synchronous request.
    """
    _make_articles(db, site, [_vec(0), _mix(0, 1, 0.9, 0.3), _mix(0, 1, 0.8, 0.4)])
    generate_suggestions(site.id, ranking_mode_override="shadow")
    ids = [s.id for s in db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()]
    assert len(ids) > 2, "need a batch large enough for per-row work to show up"

    selects: list[str] = []
    updates: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        verb = statement.lstrip().upper()
        if verb.startswith("SELECT"):
            selects.append(statement)
        elif verb.startswith("UPDATE"):
            updates.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        resp = client.post(
            "/api/v1/suggestions/bulk-review",
            json={"suggestion_ids": ids, "status": "approved"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert resp.status_code == 200
    # One SELECT for which ids exist, one constant-cost actor label for the
    # lifecycle trigger, and one UPDATE ... RETURNING for the reviews.
    assert len(selects) == 2, f"{len(selects)} SELECTs for {len(ids)} suggestions"
    assert len(updates) == 1, f"{len(updates)} UPDATEs for {len(ids)} suggestions"


def test_list_endpoints_reject_an_unbounded_limit(client, site):
    """A single request must not be able to ask for the whole table."""
    assert (
        client.get(f"/api/v1/suggestions/{site.id}", params={"limit": 10_000_000}).status_code
        == 422
    )
    assert client.get("/api/v1/sites", params={"limit": 10_000_000}).status_code == 422
    assert client.get(f"/api/v1/suggestions/{site.id}", params={"offset": -1}).status_code == 422


def test_bulk_review_enforces_batch_bounds(client, site):
    """A batch accepts the configured maximum but rejects larger or empty input."""
    # Deliberately above any real id: this test is about the size of the batch,
    # so it must not also approve a thousand rows that happen to exist. Starting
    # at 1 is how this test once overwrote real review decisions.
    UNREACHABLE_ID = 10**9

    def review(count: int) -> int:
        return client.post(
            "/api/v1/suggestions/bulk-review",
            json={
                "suggestion_ids": list(range(UNREACHABLE_ID, UNREACHABLE_ID + count)),
                "status": "approved",
            },
        ).status_code

    assert review(MAX_BULK_REVIEW) == 200
    assert review(MAX_BULK_REVIEW + 1) == 422
    # an empty batch is a client bug, not a silent no-op
    assert review(0) == 422


def test_every_list_endpoint_accepts_exactly_max_page_size(client, site):
    """Pins the boundary the dashboard pages against.

    The client walks these endpoints in pages of MAX_PAGE_SIZE, so the cap has
    to admit that exact value on every one of them — lowering it, or raising the
    client's page size past it, turns every list call into a 422.
    """
    paths = [
        "/api/v1/sites",
        f"/api/v1/sites/{site.id}/articles",
        f"/api/v1/suggestions/{site.id}",
        f"/api/v1/jobs/site/{site.id}",
        "/api/v1/alerts",
        f"/api/v1/sites/{site.id}/ingestion-runs",
    ]
    for path in paths:
        assert client.get(path, params={"limit": MAX_PAGE_SIZE}).status_code == 200, path
        assert client.get(path, params={"limit": MAX_PAGE_SIZE + 1}).status_code == 422, path
