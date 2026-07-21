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


# 'pending' lets an editor undo a decision; 'applied' is set exclusively by the
# publication worker — contract-level guarantee.
ReviewStatus = Literal["approved", "rejected", "pending"]


class SuggestionReview(BaseModel):
    status: ReviewStatus


class BulkReview(BaseModel):
    suggestion_ids: list[int]
    status: ReviewStatus
