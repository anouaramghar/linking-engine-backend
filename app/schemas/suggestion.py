from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.limits import MAX_ENGINE_PAGE_SIZE
from app.schemas.site import ArticleBrief

# The dashboard accumulates the whole queue a page at a time and then reviews
# what the editor selected, so a batch is not bounded by any single read. It
# chunks at the page size instead; the API has to agree, or a large "approve
# all" exceeds PostgreSQL's 65535-parameter limit and 500s.
MAX_BULK_REVIEW = MAX_ENGINE_PAGE_SIZE


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
BulkRuleStatus = Literal["approved", "rejected"]


class SuggestionReview(BaseModel):
    status: ReviewStatus


class BulkReview(BaseModel):
    suggestion_ids: list[int] = Field(min_length=1, max_length=MAX_BULK_REVIEW)
    status: ReviewStatus


class SuggestionCursor(BaseModel):
    """The last ordered row from a page, used to continue strictly below it."""

    score: float
    id: int


class SuggestionPage(BaseModel):
    """One cursor page of the queue.

    `total` is optional because counting the full match on every page defeats the
    cheap index continuation. Callers that need it request it once or use `/counts`.
    """

    items: list[SuggestionOut]
    total: int | None = None
    limit: int
    next_cursor: SuggestionCursor | None = None


class SuggestionCounts(BaseModel):
    """Per-status totals for one filter, so status chips cost a single query.

    `total` deliberately excludes `expired` — it is what the list endpoint returns
    when no status is given, so the two agree without the caller adding up chips.
    """

    pending: int = 0
    approved: int = 0
    rejected: int = 0
    applying: int = 0
    applied: int = 0
    expired: int = 0
    total: int = 0


class BulkReviewFilter(BaseModel):
    """Server-side form of the queue's bulk rule.

    The rule has always applied to the whole fleet rather than the rows on screen.
    Once the queue is paged, the client can no longer enumerate its own targets, so
    it sends the rule instead — which also keeps a six-figure id list off the wire.

    The wire carries the same whole-percent threshold the editor displays. The
    backend translates it to the complementary raw-score boundary shared by list,
    counts, and review queries.
    """

    status: BulkRuleStatus
    # Only reviewable rows can be matched; `applying`/`applied`/`expired` are the
    # worker's and are excluded by the guarded transition regardless.
    match_status: ReviewStatus = "pending"
    site_id: int | None = None
    all_sites: bool = False
    method: str | None = None
    threshold_percent: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _fleet_scope_must_be_deliberate(self) -> "BulkReviewFilter":
        """Omitting `site_id` reviews every site, so it has to be said out loud.

        Every other field narrows the match; a missing one widens it. That makes a
        dropped `site_id` — a client bug, a hand-written request — indistinguishable
        from a real fleet-wide rule, and the result is the entire backlog reviewed
        in one statement with no per-row record of what it used to be.
        """
        if self.site_id is None and not self.all_sites:
            raise ValueError("set site_id, or all_sites=true to review every site at once")
        if self.site_id is not None and self.all_sites:
            raise ValueError("site_id and all_sites=true contradict each other")
        return self


class BulkReviewFilterResult(BaseModel):
    """Counts for every review, plus ids while an exact undo remains practical."""

    reviewed: int
    skipped: int
    reviewed_ids: list[int] | None
    status: ReviewStatus
