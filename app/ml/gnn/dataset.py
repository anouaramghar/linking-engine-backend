"""Site graph for the GNN: nodes = articles (bge-m3 vectors as features), edges =
internal links. Tensor in/tensor out below the DB loader, so training is testable
without a database.

Taxonomies are stored from v1 for a future hetero-graph; v2 stays homogeneous —
the text embedding already carries topical signal.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, Embedding
from app.ml.evaluation.temporal_split import Pair


@dataclass
class Graph:
    node_ids: list[int]  # row index -> article id
    row_of: dict[int, int]  # article id -> row index
    x: "object"  # torch.FloatTensor [N, dim]
    edge_index: "object"  # torch.LongTensor [2, E]


def load_node_features(db: Session, site_id: int, model: str) -> tuple[list[int], "object"]:
    import torch

    rows = db.execute(
        select(Embedding.article_id, Embedding.vector)
        .join(Article, Article.id == Embedding.article_id)
        .where(Article.site_id == site_id, Embedding.model == model)
        .order_by(Embedding.article_id)
    ).all()
    if not rows:
        raise ValueError(f"no embeddings for site {site_id} — run the baseline analysis first")
    node_ids = [r[0] for r in rows]
    x = torch.tensor([list(r[1]) for r in rows], dtype=torch.float)
    return node_ids, x


def edges_to_index(pairs: list[Pair], row_of: dict[int, int]) -> "object":
    """Directed pairs -> undirected edge_index (GraphSAGE message passing is symmetric).
    Pairs whose endpoints lack an embedding are dropped."""
    import torch

    src, dst = [], []
    for s, t in pairs:
        if s in row_of and t in row_of:
            src += [row_of[s], row_of[t]]
            dst += [row_of[t], row_of[s]]
    return torch.tensor([src, dst], dtype=torch.long)


def load_graph(db: Session, site_id: int, model: str, pairs: list[Pair]) -> Graph:
    node_ids, x = load_node_features(db, site_id, model)
    row_of = {aid: i for i, aid in enumerate(node_ids)}
    return Graph(node_ids=node_ids, row_of=row_of, x=x, edge_index=edges_to_index(pairs, row_of))
