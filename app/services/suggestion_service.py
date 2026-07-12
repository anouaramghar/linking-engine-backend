"""Suggestion pipeline: encode missing embeddings, then cosine top-k -> pending suggestions
(sequence 4.2)."""

from sqlalchemy import exists, select

from app.config import settings
from app.db import SessionLocal
from app.models import Article, Embedding, Suggestion
from app.ml.baseline import top_candidates

BATCH_SIZE = 32


def _embed_missing(db, site_id: int, model: str) -> int:
    """Encode only articles without an embedding for this model — cache via unique
    (article_id, model); batch commits make interrupted runs resumable."""
    encoded = 0
    while True:
        batch = db.execute(
            select(Article.id, Article.title, Article.content_text)
            .where(
                Article.site_id == site_id,
                ~exists().where(Embedding.article_id == Article.id, Embedding.model == model),
            )
            .limit(BATCH_SIZE)
        ).all()
        if not batch:
            return encoded
        from app.ml.embeddings import encode  # lazy — heavy import

        vectors = encode([f"{title}\n{text}" for _, title, text in batch])
        for (article_id, _, _), vector in zip(batch, vectors):
            db.add(Embedding(article_id=article_id, model=model, vector=vector))
        db.commit()
        encoded += len(batch)


def generate_suggestions(site_id: int) -> dict:
    """RQ task body."""
    db = SessionLocal()
    try:
        model = settings.embedding_model
        encoded = _embed_missing(db, site_id, model)

        article_ids = db.scalars(select(Article.id).where(Article.site_id == site_id)).all()
        created = 0
        for article_id in article_ids:
            for target_id, score in top_candidates(
                db, article_id, model, settings.max_suggestions_per_article
            ):
                db.add(
                    Suggestion(
                        site_id=site_id,
                        source_article_id=article_id,
                        target_article_id=target_id,
                        method="baseline_cosine",
                        score=score,
                        status="pending",
                    )
                )
                created += 1
            db.commit()
        return {"articles_encoded": encoded, "suggestions_created": created}
    finally:
        db.close()
