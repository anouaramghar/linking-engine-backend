from collections.abc import Iterator
from secrets import compare_digest

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and (
        x_api_key is None or not compare_digest(x_api_key, settings.api_key)
    ):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")
