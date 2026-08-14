from datetime import UTC, datetime

from app.ml.evaluation.graph_metrics import (
    build_as_of_graph,
    evaluate_structural_outcomes,
)
from app.models import Article, InternalLink
from app.services.graph_service import GraphNode, compute_graph


def test_structural_outcomes_report_graph_health_changes():
    computation = compute_graph(
        [
            GraphNode(1, "/1", "source one"),
            GraphNode(2, "/2", "target two"),
            GraphNode(3, "/3", "target three"),
            GraphNode(4, "/4", "source four"),
        ],
        [(1, 2)],
    )

    summary = evaluate_structural_outcomes(
        computation,
        rankings={1: [3], 4: [2]},
        k=1,
    )

    assert summary.queries == 2
    assert summary.proposed_edges == 2
    assert summary.applied_edges == 2
    assert summary.before.orphan_count == 3
    assert summary.after.orphan_count == 2
    assert summary.to_dict()["delta"]["orphan_count"] == -1
    assert summary.to_dict()["delta"]["underlinked_count"] == -1
    assert summary.newly_connected_count == 1
    assert summary.target_concentration == 0.5


def test_as_of_graph_uses_training_edges_and_adds_measured_sources(db, site):
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    old_source = Article(
        site_id=site.id,
        external_id="old-source",
        url=f"{site.base_url}/old-source",
        title="Old source",
        content_text="old source",
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    old_target = Article(
        site_id=site.id,
        external_id="old-target",
        url=f"{site.base_url}/old-target",
        title="Old target",
        content_text="old target",
        published_at=datetime(2025, 2, 1, tzinfo=UTC),
    )
    measured_source = Article(
        site_id=site.id,
        external_id="measured-source",
        url=f"{site.base_url}/measured-source",
        title="Measured source",
        content_text="measured source",
        published_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    future_target = Article(
        site_id=site.id,
        external_id="future-target",
        url=f"{site.base_url}/future-target",
        title="Future target",
        content_text="future target",
        published_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    db.add_all([old_source, old_target, measured_source, future_target])
    db.flush()
    db.add(InternalLink(source_article_id=measured_source.id, target_article_id=old_target.id))
    db.commit()

    computation = build_as_of_graph(
        db,
        site_id=site.id,
        cutoff_at=cutoff,
        source_ids={measured_source.id},
        train_edges={(old_source.id, old_target.id)},
    )

    node_ids = {feature.article_id for feature in computation.features}
    assert {old_source.id, old_target.id, measured_source.id}.issubset(node_ids)
    assert future_target.id not in node_ids
    assert computation.edges == ((old_source.id, old_target.id),)
