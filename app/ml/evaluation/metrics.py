"""Ranking metrics for link prediction. `ranked` maps source article id -> candidate
target ids best-first; `truth` maps source article id -> the held-out real targets."""


def recall_at_k(ranked: dict[int, list[int]], truth: dict[int, set[int]], k: int = 10) -> float:
    scores = [
        len(set(ranked.get(src, [])[:k]) & targets) / len(targets)
        for src, targets in truth.items()
        if targets
    ]
    return sum(scores) / len(scores) if scores else 0.0


def mrr(ranked: dict[int, list[int]], truth: dict[int, set[int]]) -> float:
    scores = []
    for src, targets in truth.items():
        rank = next((i + 1 for i, t in enumerate(ranked.get(src, [])) if t in targets), None)
        scores.append(1.0 / rank if rank else 0.0)
    return sum(scores) / len(scores) if scores else 0.0
