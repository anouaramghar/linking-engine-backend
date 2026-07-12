"""Baseline cosine + review lifecycle — hand-crafted embeddings, no torch needed."""

from sqlalchemy import select

from app.config import settings
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
        art = Article(site_id=site.id, url=f"{site.base_url}/art-{i}", title=f"art {i}",
                      content_text=f"content {i}")
        db.add(art)
        db.flush()
        db.add(Embedding(article_id=art.id, model=settings.embedding_model, vector=vector))
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
