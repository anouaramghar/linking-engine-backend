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


class PublicationPlanOut(BaseModel):
    """One persisted, immutable edit an operator may approve.

    The HTML is the whole point: `updated_html` is byte-for-byte what will be
    sent to WordPress if this plan is approved and the live post still equals
    `original_html`. Nothing renders it again.
    """

    id: int
    status: str
    plan_hash: str
    source_article_id: int
    source_url: str
    original_html: str
    updated_html: str
    links: list[PlanLink]


class PublicationPreparationError(BaseModel):
    """A source article deliberately left out of this batch.

    It does not silently join publication later; it needs its own preparation.
    """

    source_article_id: int
    source_url: str
    message: str


class PublicationPreparationOut(BaseModel):
    """What preparation produced, and what it could not.

    `has_more` says more source articles remain unshown. It never means they
    will be included in the approval the operator is about to give.
    """

    site_id: int
    selected_suggestions: int
    plans: list[PublicationPlanOut]
    errors: list[PublicationPreparationError]
    has_more: bool


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


class PublicationQueueRequest(BaseModel):
    """The exact approved plans this click is allowed to enqueue."""

    plan_ids: list[int] = Field(min_length=1, max_length=MAX_PLANS_PER_APPROVAL)

    @field_validator("plan_ids")
    @classmethod
    def _reject_duplicate_ids(cls, plan_ids: list[int]) -> list[int]:
        if len(set(plan_ids)) != len(plan_ids):
            raise ValueError("each publication plan may be queued only once")
        return plan_ids
