from uuid import uuid4

from app.ml.hybrid import RankedCandidate
from app.models import Article, Suggestion
from app.services.editorial_feedback import (
    load_editorial_feedback,
    rerank_with_editorial_feedback,
)


def _decision(db, site, score: float, status: str, index: int) -> None:
    suffix = f"{index}-{uuid4().hex[:6]}"
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/feedback-source-{suffix}",
        title=f"Feedback source {suffix}",
        content_text="Source",
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/feedback-target-{suffix}",
        title=f"Feedback target {suffix}",
        content_text="Target",
    )
    db.add_all([source, target])
    db.flush()
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=score,
            status=status,
        )
    )


def test_editorial_feedback_reranks_from_real_site_decisions(db, site):
    site.editorial_feedback_enabled = True
    site.editorial_feedback_weight = 1.0
    site.editorial_feedback_min_samples = 10
    for index in range(5):
        _decision(db, site, 0.65, "approved", index)
    for index in range(5, 10):
        _decision(db, site, 0.95, "rejected", index)
    db.commit()

    profile = load_editorial_feedback(db, site)
    assert profile is not None
    original = [
        RankedCandidate(target_id=100, semantic_score=0.95),
        RankedCandidate(target_id=200, semantic_score=0.65),
    ]
    ranked, components = rerank_with_editorial_feedback(original, profile, weight=1.0)

    assert [candidate.target_id for candidate in ranked] == [200, 100]
    assert components[200]["feedback_rank"] == 1
    assert components[200]["score_bucket"] == "60-69"


def test_per_site_editorial_policy_api(client, site):
    response = client.put(
        f"/api/v1/sites/{site.id}/editorial-ranking-policy",
        json={
            "enabled": True,
            "min_score_percent": 72,
            "feedback_weight": 0.35,
            "min_samples": 25,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "site_id": site.id,
        "enabled": True,
        "min_score_percent": 72,
        "feedback_weight": 0.35,
        "min_samples": 25,
    }
