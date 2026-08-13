from pydantic import BaseModel, Field, field_validator

from app.connectors.base import LinkOutcome

#: A SHA-256 digest, lower-case hexadecimal. Length-checked at the schema so a
#: truncated or mistyped hash is a 422 rather than a puzzling 409.
PLAN_HASH_LENGTH = 64

#: An approval is one screenful of edits an operator actually read. A bound is
#: what keeps "approve everything" from coming back through the request body.
MAX_PLANS_PER_APPROVAL = 100


class PendingPublicationSite(BaseModel):
    """The two numbers that mean different things, kept apart.

    `selected_suggestions` is editorial intent awaiting preparation or a fresh
    preview. `approved_plans` is exact artifacts a named human has bound
    themselves to, which are the only things that may be queued.
    """

    site_id: int
    site_name: str
    platform: str
    selected_suggestions: int
    approved_plans: int
    #: Whether preparing this site's edits can succeed at all. False means no
    #: WordPress account is attached, so every source article would cost a live
    #: request and return the same 401. Said here, before the operator spends
    #: that, rather than discovered inside an empty review.
    can_publish: bool = True


class PendingPublicationPage(BaseModel):
    """One bounded page; fleet totals are included only when requested."""

    items: list[PendingPublicationSite]
    next_cursor: int | None = None
    total_sites: int | None = None
    total_selected_suggestions: int | None = None
    total_approved_plans: int | None = None


class PlanLink(BaseModel):
    """One link inside a prepared plan, exactly as it was rendered."""

    position: int
    suggestion_id: int
    target_url: str
    #: The phrase the link was written on. Displayed when the outcome is
    #: "inserted"; retained either way because it is part of the hashed artifact.
    anchor_text: str | None = None
    outcome: LinkOutcome


class PublicationPreparationError(BaseModel):
    """A source article deliberately left out of this batch.

    It does not silently join publication later; it needs its own preparation.
    """

    source_article_id: int
    source_url: str
    message: str


class PublicationPlanHtml(BaseModel):
    """Heavy exact bytes, loaded only when an operator opens advanced HTML."""

    id: int
    plan_hash: str
    original_html: str
    updated_html: str


class PlanApproval(BaseModel):
    """One plan, named by id *and* by the hash the operator was shown.

    The hash is what makes this an approval of a specific edit rather than of a
    row id whose contents may since have changed.
    """

    id: int
    plan_hash: str = Field(min_length=PLAN_HASH_LENGTH, max_length=PLAN_HASH_LENGTH)


class PlanApprovalRequest(BaseModel):
    plans: list[PlanApproval] = Field(min_length=1, max_length=MAX_PLANS_PER_APPROVAL)

    @field_validator("plans")
    @classmethod
    def _reject_duplicate_ids(cls, plans: list[PlanApproval]) -> list[PlanApproval]:
        """The same plan twice, possibly with two different hashes, is not a
        request the server can satisfy honestly."""
        ids = [plan.id for plan in plans]
        if len(set(ids)) != len(ids):
            raise ValueError("each publication plan may appear only once in an approval")
        return plans


class PlanApprovalResult(BaseModel):
    approved: list[int]
    #: Recorded on every approved plan. Echoed back so the dashboard can show
    #: who the engine believes just approved these edits.
    approved_by: str


class PublicationPreparationJobLink(BaseModel):
    """One link as the asynchronous worker reports it.

    `PlanLink` plus the sentence the link lands in. The passage is not part of
    the hashed artifact — it is read from the suggestion at preparation time —
    so it is carried here rather than added to `PlanLink`.
    """

    position: int
    suggestion_id: int
    target_url: str
    anchor_text: str | None = None
    outcome: LinkOutcome
    #: The passage the operator reads instead of raw HTML. Absent when the
    #: suggestion never carried one.
    placement_context: str | None = None


class PublicationPreparationJobPlan(BaseModel):
    """One prepared plan, without the two HTML bodies.

    The exact bytes stay behind `GET /publish/{site}/plans/{id}/html`: a batch of
    ten articles carries megabytes of markup, and a job result is polled every
    1.5 seconds until it settles.
    """

    id: int
    status: str
    plan_hash: str
    source_article_id: int
    source_url: str
    links: list[PublicationPreparationJobLink]


class PublicationPreparationJobResult(BaseModel):
    """What `prepare_publication_plans` stores in `JobRun.result`.

    The worker used to hand-build anonymous dictionaries here, so a field that
    drifted from what the dashboard reads was only discovered by an operator
    looking at a broken review. Constructing this model is the runtime boundary:
    a malformed result raises in the worker instead of being persisted.
    """

    site_id: int
    selected_suggestions: int
    plans: list[PublicationPreparationJobPlan]
    errors: list[PublicationPreparationError]
    has_more: bool


class PublicationQueueRequest(BaseModel):
    """The exact approved plans this click is allowed to enqueue."""

    plan_ids: list[int] = Field(min_length=1, max_length=MAX_PLANS_PER_APPROVAL)

    @field_validator("plan_ids")
    @classmethod
    def _reject_duplicate_ids(cls, plan_ids: list[int]) -> list[int]:
        if len(set(plan_ids)) != len(plan_ids):
            raise ValueError("each publication plan may be queued only once")
        return plan_ids
