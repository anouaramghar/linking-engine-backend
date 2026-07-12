"""Full-batch link-prediction training — CPU is enough at pilot scale (A7).

train_model is pure tensors (unit-testable); train_site wires it to the DB and
persists a checkpoint per site.
"""

from pathlib import Path

from app.config import settings

MIN_EDGES = 20  # below this a GNN can't learn anything the baseline doesn't know


def train_model(x, edge_index, epochs: int | None = None, lr: float | None = None):
    import torch
    from torch_geometric.utils import negative_sampling

    from app.ml.gnn.model import GraphSAGE

    model = GraphSAGE(x.size(1), settings.gnn_hidden_dim, settings.gnn_out_dim)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr or settings.gnn_lr)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    model.train()
    for _ in range(epochs or settings.gnn_epochs):
        optimizer.zero_grad()
        z = model(x, edge_index)
        neg = negative_sampling(edge_index, num_nodes=x.size(0), num_neg_samples=edge_index.size(1))
        logits = torch.cat([model.decode(z, edge_index), model.decode(z, neg)])
        labels = torch.cat([torch.ones(edge_index.size(1)), torch.zeros(neg.size(1))])
        loss = loss_fn(logits, labels)
        loss.backward()
        optimizer.step()
    return model, loss.item()


def checkpoint_path(site_id: int) -> Path:
    return Path(settings.model_dir) / f"gnn_site_{site_id}.pt"


def train_site(site_id: int) -> dict:
    """RQ-callable: train on ALL observed links (temporal split is for evaluation only)."""
    import torch

    from app.db import SessionLocal
    from app.ml.evaluation.temporal_split import link_pairs
    from app.ml.gnn.dataset import load_graph

    db = SessionLocal()
    try:
        pairs = [(s, t) for s, t, _ in link_pairs(db, site_id)]
        graph = load_graph(db, site_id, settings.embedding_model, pairs)
        if graph.edge_index.size(1) < MIN_EDGES:
            raise ValueError(
                f"site {site_id} has {graph.edge_index.size(1) // 2} links (<{MIN_EDGES // 2}) — "
                "too few to train a GNN, use the baseline"
            )
        model, loss = train_model(graph.x, graph.edge_index)
        path = checkpoint_path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({"state_dict": model.state_dict(), "in_dim": graph.x.size(1)}, path)
        return {"site_id": site_id, "final_loss": loss, "checkpoint": str(path)}
    finally:
        db.close()
