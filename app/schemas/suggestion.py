from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.site import ArticleBrief


class SuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    source_article: ArticleBrief
    target_article: ArticleBrief
    method: str
    score: float
    status: str
    anchor_text: str | None
    created_at: datetime


class SuggestionReview(BaseModel):
    # 'applied' is set exclusively by the publication worker — contract-level guarantee
    status: Literal["approved", "rejected"]


class BulkReview(BaseModel):
    suggestion_ids: list[int]
    status: Literal["approved", "rejected"]
