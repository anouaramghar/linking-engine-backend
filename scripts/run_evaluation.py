"""Baseline vs GNN on the temporal split — v2 exit gate: GNN must beat the baseline
by +10 points Recall@10.

Usage: uv run python scripts/run_evaluation.py SITE_ID [--k 10] [--test-fraction 0.2]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.ml.evaluation.metrics import mrr, recall_at_k  # noqa: E402
from app.ml.evaluation.temporal_split import link_pairs, temporal_split, truth_by_source  # noqa: E402


def baseline_ranked(graph, exclude: set, k: int) -> dict[int, list[int]]:
    """In-memory cosine top-k == the pgvector exact search (vectors are normalized)."""
    scores = graph.x @ graph.x.T
    scores.fill_diagonal_(float("-inf"))
    for s, t in exclude:
        if s in graph.row_of and t in graph.row_of:
            scores[graph.row_of[s], graph.row_of[t]] = float("-inf")
    top = scores.topk(min(k, scores.size(1) - 1), dim=1)
    return {
        graph.node_ids[row]: [graph.node_ids[c] for c in top.indices[row].tolist()]
        for row in range(scores.size(0))
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("site_id", type=int)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    args = parser.parse_args()

    from app.ml.gnn.dataset import load_graph
    from app.ml.gnn.predict import rank_all
    from app.ml.gnn.train import train_model

    db = SessionLocal()
    try:
        pairs = link_pairs(db, args.site_id)
        train, test = temporal_split(pairs, args.test_fraction)
        truth = truth_by_source(test)
        if not truth:
            print(f"site {args.site_id}: no test links after split — crawl more history first")
            return 1
        print(f"links: {len(train)} train / {len(test)} test, {len(truth)} test sources")

        # Both methods see only the train edges; ranking excludes them (they're known links).
        graph = load_graph(db, args.site_id, settings.embedding_model, train)
        exclude = set(train)

        base = baseline_ranked(graph, exclude, args.k)
        model, loss = train_model(graph.x, graph.edge_index)
        gnn = {
            src: [t for t, _ in targets]
            for src, targets in rank_all(model, graph, args.k, exclude).items()
        }

        rb, rg = recall_at_k(base, truth, args.k), recall_at_k(gnn, truth, args.k)
        mb, mg = mrr(base, truth), mrr(gnn, truth)
        print(f"{'':14}{'Recall@' + str(args.k):>12}{'MRR':>8}")
        print(f"{'baseline':14}{rb:12.3f}{mb:8.3f}")
        print(f"{'gnn':14}{rg:12.3f}{mg:8.3f}   (final loss {loss:.4f})")
        gate = rg - rb >= 0.10
        print(f"\ngate (+10pts Recall@{args.k}): {'PASS' if gate else 'FAIL'} ({(rg - rb) * 100:+.1f}pts)")

        # Persist for the validation dashboard (GET /stats/{site_id}/evaluation)
        out = Path(settings.model_dir) / f"eval_site_{args.site_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "site_id": args.site_id,
            "k": args.k,
            "train_links": len(train),
            "test_links": len(test),
            "baseline": {"recall_at_k": rb, "mrr": mb},
            "gnn": {"recall_at_k": rg, "mrr": mg},
            "gate_passed": gate,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }))
        return 0 if gate else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
