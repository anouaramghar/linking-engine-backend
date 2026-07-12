"""v2 checks: metrics are pure functions; the GNN part is skipped unless the ml extra
is installed (uv sync --extra ml). No DB needed — dataset/train/predict are tensor-level."""

import pytest

from app.ml.evaluation.metrics import mrr, recall_at_k
from app.ml.evaluation.temporal_split import temporal_split, truth_by_source


def test_recall_and_mrr():
    ranked = {1: [10, 11, 12], 2: [20, 21]}
    truth = {1: {11, 99}, 2: {20}}
    assert recall_at_k(ranked, truth, k=2) == pytest.approx((1 / 2 + 1 / 1) / 2)
    assert mrr(ranked, truth) == pytest.approx((1 / 2 + 1 / 1) / 2)
    assert recall_at_k({}, {}, k=10) == 0.0


def test_temporal_split_orders_by_first_seen():
    from datetime import datetime, timedelta

    t0 = datetime(2026, 1, 1)
    pairs = [(i, i + 100, t0 + timedelta(days=i)) for i in range(10)]
    train, test = temporal_split(pairs, test_fraction=0.2)
    assert len(test) == 2
    assert test == [(8, 108), (9, 109)]  # the two newest links
    assert truth_by_source(test) == {8: {108}, 9: {109}}


def test_gnn_learns_cluster_structure():
    torch = pytest.importorskip("torch")
    pytest.importorskip("torch_geometric")

    from app.ml.gnn.dataset import Graph, edges_to_index
    from app.ml.gnn.predict import rank_all
    from app.ml.gnn.train import train_model

    torch.manual_seed(0)
    # Two 10-node clusters: features = cluster indicator + noise, edges only intra-cluster.
    n = 20
    x = torch.randn(n, 8) * 0.1
    x[:10, 0] += 1.0
    x[10:, 1] += 1.0
    pairs = [(i, j) for c in (range(0, 10), range(10, 20)) for i in c for j in c if i < j]
    # Hold out one intra-cluster edge, keep the rest for training.
    held_out = pairs.pop(0)  # (0, 1)
    row_of = {i: i for i in range(n)}
    edge_index = edges_to_index(pairs, row_of)

    model, loss = train_model(x, edge_index, epochs=50, lr=0.01)
    assert loss < 0.7  # learned something beyond random (BCE at chance ≈ 0.693)

    graph = Graph(node_ids=list(range(n)), row_of=row_of, x=x, edge_index=edge_index)
    ranked = rank_all(model, graph, k=5, exclude=set(pairs))
    top_for_0 = [t for t, _ in ranked[0]]
    assert held_out[1] in top_for_0  # the held-out same-cluster edge is recovered
