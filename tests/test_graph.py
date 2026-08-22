from types import SimpleNamespace

import pytest

from app.models import Article, InternalLink, Suggestion
from app.services.graph_service import (
    GraphFeatureData,
    deterministic_rerank,
    ensure_graph_snapshot,
)


def _article(db, site, slug: str) -> Article:
    article = Article(
        site_id=site.id,
        external_id=slug,
        url=f"{site.base_url}/{slug}",
        title=slug.replace("-", " "),
        content_text=f"content for {slug}",
    )
    db.add(article)
    db.flush()
    return article


def test_graph_snapshot_is_reproducible_and_changes_with_graph_state(client, db, site):
    source = _article(db, site, "source")
    target = _article(db, site, "target")
    orphan = _article(db, site, "orphan")
    db.add(InternalLink(source_article_id=source.id, target_article_id=target.id))
    db.commit()

    first, created = ensure_graph_snapshot(db, site.id)
    assert created is True
    db.commit()
    second, created = ensure_graph_snapshot(db, site.id)
    assert created is False
    assert second.id == first.id
    assert second.graph_version == first.graph_version
    db.rollback()

    summary = client.get(f"/api/v1/sites/{site.id}/graph/summary", params={"limit": 10})
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["snapshot_id"] == first.id
    assert body["article_count"] == 3
    assert body["edge_count"] == 1
    features = {item["article_id"]: item for item in body["items"]}
    assert features[target.id]["in_degree"] == 1
    assert features[target.id]["orphan"] is False
    assert features[orphan.id]["orphan"] is True
    assert features[orphan.id]["underlinked"] is True

    db.add(InternalLink(source_article_id=source.id, target_article_id=orphan.id))
    db.commit()
    third, created = ensure_graph_snapshot(db, site.id)
    assert created is True
    assert third.id != first.id
    assert third.graph_version != first.graph_version


def test_graph_network_returns_all_active_articles_and_edges(client, db, site):
    source = _article(db, site, "source")
    target = _article(db, site, "target")
    orphan = _article(db, site, "orphan")
    db.add(InternalLink(source_article_id=source.id, target_article_id=target.id))
    db.commit()

    response = client.get(f"/api/v1/sites/{site.id}/graph/network")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["article_count"] == 3
    assert body["edge_count"] == 1
    assert {node["article_id"] for node in body["nodes"]} == {
        source.id,
        target.id,
        orphan.id,
    }
    assert {
        (edge["source_article_id"], edge["target_article_id"])
        for edge in body["edges"]
    } == {(source.id, target.id)}
    features = {node["article_id"]: node for node in body["nodes"]}
    assert features[orphan.id]["orphan"] is True
    assert body["orphan_count"] == sum(node["orphan"] for node in body["nodes"])
    assert body["underlinked_count"] == sum(node["underlinked"] for node in body["nodes"])


def test_graph_simulation_is_read_only_and_warns_on_target_concentration(client, db, site):
    source_one = _article(db, site, "source-one")
    source_two = _article(db, site, "source-two")
    target = _article(db, site, "target")
    suggestions = [
        Suggestion(
            site_id=site.id,
            source_article_id=source_one.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=0.91,
            status="approved",
        ),
        Suggestion(
            site_id=site.id,
            source_article_id=source_two.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=0.90,
            status="approved",
        ),
    ]
    db.add_all(suggestions)
    db.commit()
    ids = [suggestion.id for suggestion in suggestions]

    response = client.post(
        f"/api/v1/sites/{site.id}/graph/simulations",
        json={"suggestion_ids": ids},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied_suggestion_ids"] == ids
    assert body["before"]["active_links"] == 0
    assert body["after"]["active_links"] == 2
    assert body["after"]["orphan_count"] == 2
    assert body["newly_connected_article_ids"] == [target.id]
    assert body["target_concentration"] == 1.0
    assert any("concentration" in warning for warning in body["warnings"])
    assert (
        db.query(InternalLink)
        .filter(InternalLink.source_article_id.in_([source_one.id, source_two.id]))
        .count()
        == 0
    )


def test_graph_neighborhood_returns_focus_edges_and_one_hop_context(client, db, site):
    source = _article(db, site, "source")
    target = _article(db, site, "target")
    neighbor = _article(db, site, "neighbor")
    distant = _article(db, site, "distant")
    db.add_all(
        [
            InternalLink(source_article_id=source.id, target_article_id=neighbor.id),
            InternalLink(source_article_id=neighbor.id, target_article_id=distant.id),
        ]
    )
    suggestions = [
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=0.91,
            status="pending",
        ),
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=neighbor.id,
            method="hybrid_bm25",
            score=0.90,
            status="pending",
        ),
    ]
    db.add_all(suggestions)
    db.commit()

    response = client.post(
        f"/api/v1/sites/{site.id}/graph/neighborhood",
        json={
            "suggestion_ids": [suggestions[0].id, suggestions[1].id],
            "max_nodes": 4,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested_suggestion_ids"] == [suggestions[0].id, suggestions[1].id]
    assert body["skipped_suggestion_ids"] == []
    assert {node["article_id"] for node in body["nodes"]} == {
        source.id,
        target.id,
        neighbor.id,
        distant.id,
    }
    assert {node["article_id"] for node in body["nodes"] if node["focus"]} == {
        source.id,
        target.id,
        neighbor.id,
    }
    assert {
        (edge["source_article_id"], edge["target_article_id"])
        for edge in body["existing_edges"]
    } == {(source.id, neighbor.id), (neighbor.id, distant.id)}
    assert [edge["status"] for edge in body["proposed_edges"]] == [
        "new",
        "already_present",
    ]
    assert any("already exist" in warning for warning in body["warnings"])
    assert db.query(InternalLink).count() == 2


def test_queue_review_includes_current_graph_context(client, db, site):
    source = _article(db, site, "source")
    target = _article(db, site, "target")
    snapshot, created = ensure_graph_snapshot(db, site.id)
    assert created is True
    db.commit()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.88,
        status="pending",
    )
    db.add(suggestion)
    db.commit()

    response = client.get("/api/v1/suggestions", params={"site_id": site.id})
    assert response.status_code == 200, response.text
    body = response.json()
    graph = body["items"][0]["score_components"]["graph"]
    assert graph["snapshot_id"] == snapshot.id
    assert graph["target_orphan"] is True
    assert graph["target_in_degree"] == 0
    assert graph["mode"] == "context_only"


def test_graph_reranker_keeps_a_materially_more_relevant_candidate_first():
    candidates = [
        SimpleNamespace(target_id=10, semantic_score=0.90),
        SimpleNamespace(target_id=11, semantic_score=0.70),
        SimpleNamespace(target_id=12, semantic_score=0.49),
    ]
    features = {
        10: GraphFeatureData(10, "/10", "connected", 4, 1, False, False, False, False, 0.1, 0.4),
        11: GraphFeatureData(11, "/11", "orphan", 0, 1, True, True, False, False, 0.1, 0.0),
        12: GraphFeatureData(12, "/12", "weak orphan", 0, 1, True, True, False, False, 0.1, 0.0),
    }

    ordered, metadata = deterministic_rerank(
        candidates,
        features,
        source_article_id=10,
        mode="active",
        max_relevance_boost=0.03,
        minimum_relevance=0.50,
    )

    assert [candidate.target_id for candidate in ordered] == [10, 11, 12]
    assert metadata[11].adjustment == 0.03
    assert metadata[12].adjustment == 0.0


def test_graph_reranker_can_use_a_small_structural_opportunity_within_relevance_margin():
    candidates = [
        SimpleNamespace(target_id=10, semantic_score=0.85),
        SimpleNamespace(target_id=11, semantic_score=0.83),
    ]
    features = {
        10: GraphFeatureData(10, "/10", "connected", 3, 1, False, False, False, False, 0.1, 0.3),
        11: GraphFeatureData(11, "/11", "orphan", 0, 1, True, True, False, False, 0.1, 0.0),
    }

    ordered, metadata = deterministic_rerank(
        candidates,
        features,
        source_article_id=10,
        mode="active",
        max_relevance_boost=0.03,
    )

    assert [candidate.target_id for candidate in ordered] == [11, 10]
    assert metadata[11].final_rank == 1
    assert metadata[11].applied is True


@pytest.mark.parametrize("mode", ("off", "shadow"))
def test_graph_reranker_preserves_baseline_order_when_disabled(mode):
    """Disabled graph modes must not change the BM25 order they observe."""
    candidates = [
        SimpleNamespace(target_id=11, semantic_score=0.50),
        SimpleNamespace(target_id=10, semantic_score=0.90),
    ]
    features = {
        10: GraphFeatureData(10, "/10", "connected", 4, 1, False, False, False, False, 0.1, 0.4),
        11: GraphFeatureData(11, "/11", "orphan", 0, 1, True, True, False, False, 0.1, 0.0),
    }

    ordered, metadata = deterministic_rerank(
        candidates,
        features,
        source_article_id=10,
        mode=mode,
        max_relevance_boost=0.03,
    )

    assert [candidate.target_id for candidate in ordered] == [11, 10]
    assert metadata[11].baseline_rank == metadata[11].final_rank == 1
    assert metadata[10].baseline_rank == metadata[10].final_rank == 2
    assert all(item.adjustment == 0.0 and not item.applied for item in metadata.values())
