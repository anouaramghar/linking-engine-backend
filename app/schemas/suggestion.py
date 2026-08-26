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

# Long enough for a full article title, short enough that the term stays a term.
# A trigram index degrades toward a sequential scan as the pattern grows, so this
# is a performance bound as much as a validation one.
MAX_SEARCH_TERM = 200

TargetOrigin = Literal["internal", "content_pool", "web_search"]

RejectionReason = Literal[
    "not_relevant",
    "wrong_target",
    "bad_anchor",
    "bad_placement",
    "already_covered",
    "duplicate",
    "other",
]

ExposureSurface = Literal["queue", "preview"]


class SuggestionTargetBrief(BaseModel):
    """A stored article or a direct external-search target."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None
    title: str
    url: str


class SuggestionEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    suggestion_id: int
    event_type: str
    actor: str
    details: dict
    created_at: datetime


class TraceEventOut(SuggestionEventOut):
    trace_id: str
    site_id: int
    site_name: str
    source_title: str
    target_title: str
    suggestion_status: str
    publish_error: str | None = None


class TraceEventPage(BaseModel):
    items: list[TraceEventOut]
    total: int
    limit: int
    offset: int


class SuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trace_id: str
    site_id: int
    source_article: ArticleBrief
    target_article: SuggestionTargetBrief
    #: Where the target lives relative to the site being reviewed.
    target_origin: TargetOrigin
    #: The site that owns the target article; useful when the target is external.
    target_site_name: str
    method: str
    #: Cosine semantic similarity, whichever method selected the row.
    score: float
    #: How this row was chosen, when the method records it. For `hybrid_bm25`:
    #: the BM25 score that ordered it, its fusion and per-retriever ranks, and
    #: the recipe names. Null for `baseline_cosine`, whose score already is its
    #: whole explanation.
    score_components: dict | None = None
    provider: str | None = None
    provider_request_id: str | None = None
    provider_score: float | None = None
    search_query: str | None = None
    external_snippet: str | None = None
    status: str
    anchor_text: str | None
    publish_outcome: str | None = None
    publish_attempts: int = 0
    publish_error: str | None = None
    shown_at: datetime | None = None
    last_shown_at: datetime | None = None
    exposure_count: int = 0
    reviewer_id: str | None = None
    rejection_reason: RejectionReason | None = None
    retrieval_version: str | None = None
    ranking_version: str | None = None
    final_rank: int | None = None
    created_at: datetime


class PlacementOut(BaseModel):
    """Where in the source article the link would go.

    Generated on demand rather than during analysis, so this is not part of
    `SuggestionOut` — a queue page must not pay for a model call per row.

    `found` is the field to branch on. A null `placement_context` with
    `found=false` is a real answer: the model read the article and no passage
    fit. The client renders that, not a spinner.
    """

    suggestion_id: int
    found: bool
    #: A passage copied verbatim from the source article, or null.
    placement_context: str | None
    #: A contiguous substring of `placement_context`, so a client can highlight
    #: it by searching the context rather than by trusting an offset.
    anchor_text: str | None
    #: Which model produced this, for traceability once several have run.
    llm_model: str | None
    generated_at: datetime


# 'pending' lets an editor undo a decision; 'applied' is set exclusively by the
# publication worker — contract-level guarantee.
ReviewStatus = Literal["approved", "rejected", "pending"]
BulkRuleStatus = Literal["approved", "rejected"]


class SuggestionReview(BaseModel):
    status: ReviewStatus
    rejection_reason: RejectionReason | None = None

    @model_validator(mode="after")
    def reason_only_applies_to_rejection(self) -> "SuggestionReview":
        if self.rejection_reason is not None and self.status != "rejected":
            raise ValueError("rejection_reason is only valid when status is rejected")
        return self


class BulkReview(BaseModel):
    suggestion_ids: list[int] = Field(min_length=1, max_length=MAX_BULK_REVIEW)
    status: ReviewStatus
    rejection_reason: RejectionReason | None = None

    @model_validator(mode="after")
    def reason_only_applies_to_rejection(self) -> "BulkReview":
        if self.rejection_reason is not None and self.status != "rejected":
            raise ValueError("rejection_reason is only valid when status is rejected")
        return self


class SuggestionExposure(BaseModel):
    suggestion_ids: list[int] = Field(min_length=1, max_length=MAX_BULK_REVIEW)
    surface: ExposureSurface = "queue"


class SuggestionExposureResult(BaseModel):
    exposed: int


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
    # Quarantined after repeated publication failures. Counted in `total` because
    # the list endpoint returns these rows, and a chip the editor cannot see is
    # how a stuck suggestion stays stuck.
    failed: int = 0
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
    rejection_reason: RejectionReason | None = None
    # Only reviewable rows can be matched; `applying`/`applied`/`expired` are the
    # worker's and are excluded by the guarded transition regardless.
    match_status: ReviewStatus = "pending"
    site_id: int | None = None
    all_sites: bool = False
    method: str | None = None
    threshold_percent: int = Field(ge=0, le=100)
    # The rule carries every filter the queue itself can apply, so "accept the
    # 412 shown" can be true rather than approximately true. Each of these only
    # ever narrows the match, so an older client that omits them keeps its
    # existing fleet-wide behaviour.
    q: str | None = Field(default=None, max_length=MAX_SEARCH_TERM)
    target_origin: TargetOrigin | None = None
    exclude_reciprocal: bool = False

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
        if self.rejection_reason is not None and self.status != "rejected":
            raise ValueError("rejection_reason is only valid when status is rejected")
        return self


class BulkReviewFilterResult(BaseModel):
    """Counts for every review and a durable exact-undo operation."""

    reviewed: int
    skipped: int
    reviewed_ids: list[int] | None
    undo_operation_id: str | None
    status: ReviewStatus


class BulkReviewUndoResult(BaseModel):
    restored: int
    skipped: int
    status: ReviewStatus
    already_undone: bool = False
