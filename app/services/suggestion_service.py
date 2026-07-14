"""Suggestion pipeline: encode missing embeddings, then cosine top-k -> pending suggestions
(sequence 4.2)."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import exists, func, select

from app.config import settings
from app.db import SessionLocal, engine
from app.models import Article, Embedding, Suggestion
from app.ml.baseline import top_candidates

BATCH_SIZE = 32
_ANALYSIS_LOCK_NAMESPACE = 0x4C4D


@contextmanager
def _site_analysis_lock(site_id: int) -> Iterator[None]:
    # Batch commits use a separate work session. This dedicated transaction therefore holds
    # the lock for the entire analysis and lets PostgreSQL release it reliably on every exit.
    with engine.begin() as lock_connection:
        lock_connection.execute(
            select(func.pg_advisory_xact_lock(_ANALYSIS_LOCK_NAMESPACE, site_id))
        ).scalar_one()
        yield


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
    with _site_analysis_lock(site_id):
        db = SessionLocal()
        try:
            model = settings.embedding_model
            encoded = _embed_missing(db, site_id, model)

            article_ids = db.scalars(select(Article.id).where(Article.site_id == site_id)).all()
            existing_counts = dict(
                db.execute(
                    select(Suggestion.source_article_id, func.count())
                    .where(Suggestion.site_id == site_id)
                    .group_by(Suggestion.source_article_id)
                ).all()
            )
            created = 0
            for article_id in article_ids:
                remaining = settings.max_suggestions_per_article - existing_counts.get(
                    article_id, 0
                )
                if remaining <= 0:
                    continue
                for target_id, score in top_candidates(db, article_id, model, remaining):
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
