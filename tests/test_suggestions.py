"""Baseline cosine + review lifecycle — hand-crafted embeddings, no torch needed."""

import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Lock

from sqlalchemy import event, select

from app.config import settings
from app.db import engine
from app.models import Article, Embedding, InternalLink, Suggestion
from app.models.article import EMBEDDING_DIM
from app.services.suggestion_service import generate_suggestions


def _vec(direction: int, weight: float = 1.0) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[direction] = weight
    return v


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
