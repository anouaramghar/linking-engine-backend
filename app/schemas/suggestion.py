from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.site import ArticleBrief

# The dashboard accumulates the whole queue a page at a time and then reviews
# what the editor selected, so a batch is not bounded by any single read. It
# chunks at the page size instead; the API has to agree, or a large "approve
# all" exceeds PostgreSQL's 65535-parameter limit and 500s. Pinned equal to
# MAX_PAGE_SIZE by `test_bulk_review_bound_matches_the_page_size`.
MAX_BULK_REVIEW = 1000


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
    suggestion_ids: list[int] = Field(min_length=1, max_length=MAX_BULK_REVIEW)
    status: ReviewStatus
