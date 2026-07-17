import hashlib
import re

import pytest
from sqlalchemy import event, func, select

from app.config import settings
from app.db import engine
from app.models import Article, Embedding
from app.models.article import EMBEDDING_DIM
from app.services.suggestion_service import _embed_missing


def _fingerprint(title: str, text: str) -> str:
    return hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()


def _make_article(
    db,
    site,
    *,
    title: str = "Original title",
    text: str = "Original text",
    slug: str = "embedding-refresh",
):
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/{slug}",
        title=title,
        content_text=text,
    )
    db.add(article)
    db.flush()
    return article


def _require_embedding_metadata() -> None:
    expected = {"content_fingerprint", "input_recipe_version", "vector_size"}
    assert expected <= set(Embedding.__table__.columns.keys()), (
        "embedding metadata columns are missing"
    )


@pytest.mark.parametrize("changed_field", ["title", "content_text"])
def test_changed_encode_input_refreshes_existing_embedding(db, site, monkeypatch, changed_field):
    _require_embedding_metadata()
    article = _make_article(db, site)
    old_vector = [0.0] * EMBEDDING_DIM
    embedding = Embedding(
        article_id=article.id,
        model=settings.embedding_model,
        vector=old_vector,
        content_fingerprint=_fingerprint(article.title, article.content_text),
        input_recipe_version=1,
        vector_size=EMBEDDING_DIM,
    )
    db.add(embedding)
    db.commit()
    embedding_id = embedding.id

    setattr(article, changed_field, f"Changed {changed_field}")
    db.commit()
    encode_input = f"{article.title}\n{article.content_text}"
    new_vector = [1.0] * EMBEDDING_DIM
    calls = []

    def fake_encode(texts):
        calls.append(texts)
        return [new_vector]

    monkeypatch.setattr("app.ml.embeddings.encode", fake_encode)

    assert _embed_missing(db, site.id, settings.embedding_model) == 1

    db.refresh(embedding)
    assert calls == [[encode_input]]
    assert embedding.id == embedding_id
    assert list(embedding.vector) == new_vector
    assert embedding.content_fingerprint == hashlib.sha256(encode_input.encode()).hexdigest()
    assert embedding.input_recipe_version == 1
    assert embedding.vector_size == EMBEDDING_DIM


def test_current_embedding_is_not_encoded_or_rewritten(db, site, monkeypatch):
    _require_embedding_metadata()
    article = _make_article(db, site)
    vector = [0.25] * EMBEDDING_DIM
    embedding = Embedding(
        article_id=article.id,
        model=settings.embedding_model,
        vector=vector,
        content_fingerprint=_fingerprint(article.title, article.content_text),
        input_recipe_version=1,
        vector_size=EMBEDDING_DIM,
    )
    db.add(embedding)
    db.commit()
    embedding_id = embedding.id
    writes = []

    def fail_encode(_texts):
        raise AssertionError("current content must not be encoded")

    def record_embedding_writes(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT INTO EMBEDDINGS", "UPDATE EMBEDDINGS")):
            writes.append(statement)

    monkeypatch.setattr("app.ml.embeddings.encode", fail_encode)
    event.listen(engine, "before_cursor_execute", record_embedding_writes)
    try:
        assert _embed_missing(db, site.id, settings.embedding_model) == 0
    finally:
        event.remove(engine, "before_cursor_execute", record_embedding_writes)

    db.refresh(embedding)
    assert writes == []
    assert embedding.id == embedding_id
    assert list(embedding.vector) == vector


@pytest.mark.parametrize("existing_embedding", [False, True])
def test_wrong_vector_size_raises_before_embedding_write(db, site, monkeypatch, existing_embedding):
    _require_embedding_metadata()
    articles = [
        _make_article(db, site, title="First", text="First text", slug="dimension-first"),
        _make_article(db, site, title="Second", text="Second text", slug="dimension-second"),
    ]
    original_vectors = [[0.25] * EMBEDDING_DIM, [0.5] * EMBEDDING_DIM]
    if existing_embedding:
        db.add_all(
            [
                Embedding(
                    article_id=article.id,
                    model=settings.embedding_model,
                    vector=vector,
                    content_fingerprint=None,
                    input_recipe_version=None,
                    vector_size=None,
                )
                for article, vector in zip(articles, original_vectors)
            ]
        )
    db.commit()
    article_ids = [article.id for article in articles]
    produced_size = EMBEDDING_DIM - 1
    valid_vector = [0.75] * EMBEDDING_DIM
    calls = []
    writes = []

    def fake_encode(texts):
        calls.append(texts)
        return [valid_vector, [0.0] * produced_size]

    def record_embedding_writes(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT INTO EMBEDDINGS", "UPDATE EMBEDDINGS")):
            writes.append(statement)

    monkeypatch.setattr("app.ml.embeddings.encode", fake_encode)
    message = re.escape(settings.embedding_model) + rf".*{produced_size}.*{EMBEDDING_DIM}"
    event.listen(engine, "before_cursor_execute", record_embedding_writes)
    try:
        with pytest.raises(ValueError, match=message):
            _embed_missing(db, site.id, settings.embedding_model)
        assert calls == [
            [
                f"{articles[0].title}\n{articles[0].content_text}",
                f"{articles[1].title}\n{articles[1].content_text}",
            ]
        ]
        assert writes == []
        assert not db.new
        assert not db.dirty
    finally:
        event.remove(engine, "before_cursor_execute", record_embedding_writes)
        db.rollback()

    stored = db.scalars(
        select(Embedding)
        .where(
            Embedding.article_id.in_(article_ids),
            Embedding.model == settings.embedding_model,
        )
        .order_by(Embedding.article_id)
    ).all()
    if existing_embedding:
        assert len(stored) == 2
        for embedding, original_vector in zip(stored, original_vectors):
            assert list(embedding.vector) == original_vector
            assert embedding.content_fingerprint is None
            assert embedding.input_recipe_version is None
            assert embedding.vector_size is None
    else:
        assert stored == []


def test_nullable_embedding_metadata_is_refreshed_lazily(db, site, monkeypatch):
    _require_embedding_metadata()
    article = _make_article(db, site)
    embedding = Embedding(
        article_id=article.id,
        model=settings.embedding_model,
        vector=[0.0] * EMBEDDING_DIM,
        content_fingerprint=None,
        input_recipe_version=None,
        vector_size=None,
    )
    db.add(embedding)
    db.commit()
    embedding_id = embedding.id
    vector = [0.75] * EMBEDDING_DIM
    monkeypatch.setattr("app.ml.embeddings.encode", lambda _texts: [vector])

    assert _embed_missing(db, site.id, settings.embedding_model) == 1

    db.refresh(embedding)
    assert embedding.id == embedding_id
    assert list(embedding.vector) == vector
    assert embedding.content_fingerprint == _fingerprint(article.title, article.content_text)
    assert embedding.input_recipe_version == 1
    assert embedding.vector_size == EMBEDDING_DIM


def test_only_active_articles_are_refreshed(db, site, monkeypatch):
    _require_embedding_metadata()
    article = _make_article(db, site)
    article.is_active = False
    embedding = Embedding(
        article_id=article.id,
        model=settings.embedding_model,
        vector=[0.0] * EMBEDDING_DIM,
        content_fingerprint=None,
        input_recipe_version=None,
        vector_size=None,
    )
    db.add(embedding)
    db.commit()
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda _texts: pytest.fail("inactive articles must not be encoded"),
    )

    assert _embed_missing(db, site.id, settings.embedding_model) == 0
    assert (
        db.scalar(
            select(func.count()).select_from(Embedding).where(Embedding.article_id == article.id)
        )
        == 1
    )
