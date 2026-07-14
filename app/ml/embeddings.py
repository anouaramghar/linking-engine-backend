"""bge-m3 encoding — CPU by default (A7), lazy import so the API runs without the ml extra.

Install the heavy stack with: uv sync --extra ml
"""

from app.config import settings

_model = None


def get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers not installed — run: uv sync --extra ml"
            ) from e
        _model = SentenceTransformer(settings.embedding_model, device=settings.embedding_device)
    return _model


def encode(texts: list[str]) -> list[list[float]]:
    # normalized vectors -> cosine similarity == dot product, and pgvector <=> is exact
    return get_model().encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()
