"""GNN suggestion pipeline: load (or train) the site checkpoint, score every pair,
keep top-k per article with the same editorial filters as the baseline.

ponytail: dense N x N scoring — fine for pilot sites (<10k articles); switch to
chunked or ANN scoring when a site outgrows memory.
"""

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Article, Suggestion
from app.ml.evaluation.temporal_split import link_pairs
from app.ml.gnn.dataset import Graph, load_graph
from app.ml.gnn.train import checkpoint_path, train_site


def rank_all(
    model, graph: Graph, k: int, exclude: set[tuple[int, int]]
) -> dict[int, list[tuple[int, float]]]:
    """Top-k (target article id, sigmoid score) per source article id, best first."""
    import torch

    model.eval()
    with torch.no_grad():
        z = model(graph.x, graph.edge_index)
        scores = z @ z.T
    scores.fill_diagonal_(float("-inf"))
    for s, t in exclude:
        if s in graph.row_of and t in graph.row_of:
            scores[graph.row_of[s], graph.row_of[t]] = float("-inf")
    k = min(k, scores.size(1) - 1)
    if k <= 0:
        return {}
    top = scores.topk(k, dim=1)
    probs = torch.sigmoid(top.values)
    return {
        graph.node_ids[row]: [
            (graph.node_ids[c], float(p))
            for c, p in zip(top.indices[row].tolist(), probs[row].tolist())
        ]
        for row in range(scores.size(0))
    }


def _load_model(site_id: int):
    import torch

    from app.ml.gnn.model import GraphSAGE

    path = checkpoint_path(site_id)
    if not path.exists():
        train_site(site_id)
    ckpt = torch.load(path, weights_only=True)
    model = GraphSAGE(ckpt["in_dim"], settings.gnn_hidden_dim, settings.gnn_out_dim)
    model.load_state_dict(ckpt["state_dict"])
    return model


def generate_gnn_suggestions(site_id: int) -> dict:
    """RQ task body — same contract as the baseline generate_suggestions."""
    db = SessionLocal()
    try:
        from app.services.suggestion_service import _embed_missing

        encoded = _embed_missing(db, site_id, settings.embedding_model)

        pairs = [(s, t) for s, t, _ in link_pairs(db, site_id)]
        graph = load_graph(db, site_id, settings.embedding_model, pairs)
        model = _load_model(site_id)

        already_suggested = set(
            db.execute(
                select(Suggestion.source_article_id, Suggestion.target_article_id)
                .join(Article, Article.id == Suggestion.source_article_id)
                .where(Article.site_id == site_id)
            ).all()
        )
        exclude = set(pairs) | already_suggested
        ranked = rank_all(model, graph, settings.max_suggestions_per_article, exclude)

        created = 0
        for source_id, targets in ranked.items():
            for target_id, score in targets:
                db.add(
                    Suggestion(
                        site_id=site_id,
                        source_article_id=source_id,
                        target_article_id=target_id,
                        method="gnn_graphsage",
                        score=score,
                        status="pending",
                    )
                )
                created += 1
        db.commit()
        return {"articles_encoded": encoded, "suggestions_created": created}
    finally:
        db.close()
