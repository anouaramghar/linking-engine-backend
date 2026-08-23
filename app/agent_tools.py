"""The one read-only tool registry behind both agent surfaces.

The MCP server (``app.mcp_server``) and the dashboard assistant
(``POST /api/v1/agent/chat``) both execute *these* handlers — there is no
second implementation anywhere. That is the whole point of the module: an
answer an agent gives is computed by exactly the code path the REST API uses,
including tenant scoping and site authorization, because most handlers below
call the route functions themselves rather than re-deriving their queries.

Every tool here reads. A tool may stage an exact, typed proposal, but it never
executes that proposal: the dashboard renders a confirmation affordance and
the editor performs the audited REST mutation. Publication, credential, and
destructive operations are not staged at all.

Handlers return plain JSON-safe dicts. Failures are returned as ``{"error":
..., "status": ...}`` rather than raised, because the chat loop reads outcomes
as data and a model copes better with a message it can quote than with an
exception. Surfaces that have a failure channel of their own translate on the
way out — see ``error_of`` and ``app.mcp_server``, which turns that shape into
an MCP ``isError`` result carrying the same text.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from typing import Any, Callable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.api.routes.evaluation import get_evaluation_metrics
from app.api.routes.ingestion import latest_ingestion_run
from app.api.routes.jobs import list_active_job_runs, list_job_runs
from app.api.routes.suggestions import (
    MAX_SEARCH_TERM,
    _queue_conditions,
    _tenant_scope,
    count_suggestions,
    list_suggestion_page,
    list_trace_events,
)
from app.api.routes.publish import pending_publication_sites, publication_status
from app.api.routes.sites import _site_counts, get_site, list_sites
from app.models import (
    Alert,
    Article,
    IngestionDiagnostic,
    IngestionRun,
    InternalLink,
    JobRun,
    PublicationPlan,
    Site,
    Suggestion,
)
from app.services.authorization import (
    POOL_PLATFORM,
    Principal,
    authorize_site,
    authorize_site_read,
    require_admin_principal,
    tenant_site_filter,
)

logger = logging.getLogger(__name__)


#: The closed vocabularies an agent may filter on. Each mirrors a column
#: definition, not a guess: ``SuggestionStatus`` and ``SuggestionMethod`` in
#: app/models/suggestion.py, and ``_QUEUES`` in app/services/job_service.py.
#:
#: Declared as literals rather than derived from those objects because
#: ``Literal`` members must be literal, and the JSON Schema a model reads is
#: built from them. ``test_filter_vocabularies_match_the_database`` is what
#: keeps the two equal. Getting this wrong is not a validation nicety: an
#: unconstrained filter accepts an invented value, matches nothing, and answers
#: a confident zero that reads exactly like "there are none of these".
QueueStatus = Literal["pending", "approved", "rejected", "expired", "applying", "applied", "failed"]
SuggestionMethodName = Literal["baseline_cosine", "hybrid_bm25", "gnn_graphsage", "external_search"]
JobKind = Literal["ingestion", "analysis", "publication_preparation", "publication"]


class ListSitesArgs(BaseModel):
    search: str | None = Field(None, description="Filter by name, URL, or platform substring.")


class QueueCountsArgs(BaseModel):
    site_id: int | None = Field(None, description="Restrict to one site.")
    method: SuggestionMethodName | None = Field(None, description="Suggestion method filter.")
    q: str | None = Field(None, description="Title/URL text filter.")


class SearchQueueArgs(BaseModel):
    """The queue's filters, bounded for an agent.

    Every bound here is stated on the model rather than borrowed from the
    route: ``list_suggestion_page`` declares its limits with ``Query(...)``,
    and calling a route function directly leaves those descriptors unresolved,
    so nothing would validate ``min_percent`` or the length of ``q``.
    """

    site_id: int | None = Field(None, description="Restrict to one site.")
    status: QueueStatus = "pending"
    q: str | None = Field(None, max_length=MAX_SEARCH_TERM, description="Title/URL text filter.")
    method: SuggestionMethodName | None = Field(None, description="Suggestion method filter.")
    min_percent: int | None = Field(
        None, ge=0, le=100, description="Only rows at or above this whole-percent rank score."
    )
    max_percent: int | None = Field(
        None, ge=0, le=100, description="Only rows below this whole-percent rank score."
    )
    target_origin: Literal["internal", "content_pool", "web_search"] | None = Field(
        None, description="Where the link target came from."
    )
    exclude_reciprocal: bool = Field(
        False, description="Drop rows whose reverse pair already scores higher."
    )
    cursor: str | None = Field(
        None,
        description=(
            "Continue after a previous page: pass the `next_cursor` string that "
            "page returned, with the same filters."
        ),
    )
    limit: int = Field(10, ge=1, le=50)


class EvaluationArgs(BaseModel):
    site_id: int | None = Field(None, description="Optional single-site filter.")
    date_from: str | None = Field(
        None,
        description=(
            "ISO-8601 date or datetime; filters the generated-suggestion cohort. "
            "A bare date is read as midnight UTC."
        ),
    )
    date_to: str | None = Field(
        None, description="ISO-8601 date or datetime; a bare date is read as midnight UTC."
    )


class SiteStatusArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description="Which site to report on. Omit it to use the only connected site.",
    )


class SuggestionHistoryArgs(BaseModel):
    """Identify the suggestion either way round.

    ``explain_suggestion`` returns a ``trace_id``, which is what the route
    filters on, but a model usually has only the numeric id it was given — so
    accept that too and resolve it here rather than making the caller chain
    two tools to ask one question.
    """

    suggestion_id: int | None = Field(None, description="Resolved to its trace_id.")
    trace_id: str | None = Field(None, max_length=MAX_SEARCH_TERM)
    site_id: int | None = Field(None, description="Every event on one site.")
    # Left open on purpose. Unlike status, method, and job kind, the event
    # vocabulary has no column enum behind it (``suggestion_events.event_type``
    # is a plain String(30)) and is written from both the application and a
    # database trigger, so any list here would be a guess that silently blocks
    # real events.
    event_type: str | None = Field(
        None, max_length=30, description="Restrict to one kind of event."
    )
    limit: int = Field(20, ge=1, le=50)
    offset: int = Field(0, ge=0)


class IngestionDiagnosticsArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description="Which crawled site. Omit it to use the only connected site.",
    )
    run_id: int | None = Field(None, description="Defaults to the most recent ingestion run.")
    reason_code: str | None = Field(
        None, max_length=80, description="Only example rows with this reason."
    )
    examples: int = Field(10, ge=0, le=25, description="Example URLs to return per call.")


class SiteJobsArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description="Which site's jobs to list. Omit it to use the only connected site.",
    )
    kind: JobKind | None = Field(None, description="Restrict to one job kind.")
    limit: int = Field(15, ge=1, le=50)


class PublicationStatusArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description=("One site's publication state. Omit for every site with work waiting."),
    )
    limit: int = Field(20, ge=1, le=50, description="Sites returned when site_id is omitted.")


class GraphSummaryArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description="Which site's link graph to summarize. Omit it to use the only connected site.",
    )


class ActiveJobsArgs(BaseModel):
    limit: int = Field(15, ge=1, le=25)


class FindArticlesArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description="Which site to search. Omit it to use the only connected site.",
    )
    q: str | None = Field(
        None, max_length=MAX_SEARCH_TERM, description="Title or URL substring to match."
    )
    orphans: bool = Field(False, description="Only articles with no active incoming link.")
    limit: int = Field(15, ge=1, le=25)


class PreviewBulkReviewArgs(BaseModel):
    """A bulk rule the operator may confirm — the tool itself only counts."""

    action: Literal["approve", "reject"]
    # Mirrors BulkReviewFilter's deliberate-scope rule: one or the other, never
    # neither, and fleet scope stays admin-only exactly like the REST route.
    site_id: int | None = Field(None, description="Apply to this site only.")
    all_sites: bool = Field(False, description="Apply to every site (admin only).")
    threshold_percent: int = Field(
        ge=0,
        le=100,
        description=(
            "Whole-percent rank-score boundary — the same number the queue is "
            "ordered by and the dashboard card shows, not cosine similarity. "
            "approve = match pending rows at or above it; reject = match "
            "pending rows below it."
        ),
    )
    rejection_reason: (
        Literal[
            "not_relevant",
            "wrong_target",
            "bad_anchor",
            "bad_placement",
            "already_covered",
            "duplicate",
            "other",
        ]
        | None
    ) = Field(None, description="Required by the endpoint when action=reject.")
    q: str | None = Field(None, max_length=MAX_SEARCH_TERM)
    method: SuggestionMethodName | None = None
    target_origin: Literal["internal", "content_pool", "web_search"] | None = None
    exclude_reciprocal: bool = False

    @model_validator(mode="after")
    def _rule_must_be_executable(self) -> "PreviewBulkReviewArgs":
        """Refuse here what ``BulkReviewFilter`` would refuse on submission.

        These lived in the handler, so they were absent from the JSON Schema
        and a model met them only by failing. Worse, the tool was the looser of
        the two: it accepted ``site_id`` *and* ``all_sites`` together, which the
        endpoint rejects — so it could preview a rule the operator could not
        then confirm.
        """
        if self.site_id is None and not self.all_sites:
            raise ValueError("set site_id, or all_sites=true to review every site at once")
        if self.site_id is not None and self.all_sites:
            raise ValueError("site_id and all_sites=true contradict each other")
        if self.action == "reject" and self.rejection_reason is None:
            raise ValueError("rejection_reason is required when action is reject")
        if self.rejection_reason is not None and self.action != "reject":
            raise ValueError("rejection_reason is only valid when action is reject")
        return self


class PreviewSuggestionReviewArgs(BaseModel):
    """One editorial decision the operator may confirm; this tool only stages."""

    suggestion_id: int = Field(ge=1, description="The suggestion shown in the review queue.")
    action: Literal["approve", "reject"]
    rejection_reason: (
        Literal[
            "not_relevant",
            "wrong_target",
            "bad_anchor",
            "bad_placement",
            "already_covered",
            "duplicate",
            "other",
        ]
        | None
    ) = Field(None, description="Required when action=reject.")

    @model_validator(mode="after")
    def _decision_must_be_executable(self) -> "PreviewSuggestionReviewArgs":
        if self.action == "reject" and self.rejection_reason is None:
            raise ValueError("rejection_reason is required when action is reject")
        if self.rejection_reason is not None and self.action != "reject":
            raise ValueError("rejection_reason is only valid when action is reject")
        return self


class ExplainSuggestionArgs(BaseModel):
    suggestion_id: int = Field(description="The numeric id shown in the review queue.")


class OpsDigestArgs(BaseModel):
    include_acknowledged_alerts: bool = Field(
        False, description="Include alerts an operator already acknowledged."
    )


#: ---------------------------------------------------------------------------
#: Result shapes for the tools that return a count beside a list.
#:
#: These are the tools where one number was mistaken for another, so they are
#: the ones worth publishing a contract for. The contract is what an MCP client
#: reads *before* calling: ``inputSchema`` stops a model inventing an argument,
#: ``outputSchema`` stops it inventing — or misreading — a field.
#:
#: Handlers still return plain dicts. These models describe that dict and are
#: checked against it rather than replacing it, because the same dict is what
#: the chat loop reads and what the tests assert on.


class Page(BaseModel):
    """What one call returned, kept apart from what matched.

    There is deliberately no row count here. It was exactly ``len(rows)`` of the
    list beside it, and a second integer saying what the list already says is
    the field a model reaches for when asked "how many" — first at the top level
    as ``returned``, then, after grouping, as ``page.returned``. Twice is
    enough: a number that cannot be seen cannot be reported, and nothing is
    lost, because counting the array gives it back exactly.

    What remains is not derivable and is not a count: whether more rows match,
    and how to ask for them.
    """

    has_more: bool = Field(description="True when more rows match than were returned.")
    next_cursor: str | None = Field(
        None,
        description="Absent on the last page. Pass back as `cursor` with the same filters.",
    )
    offset: int | None = Field(None, description="Rows skipped before this page.")


class SearchQueueResult(BaseModel):
    match_count: int | None = Field(
        None,
        description=(
            "Suggestions matching the filters in total — the answer to 'how many'. "
            "Counted on the first page only, so it is absent when continuing from a "
            "cursor; the count has not changed."
        ),
    )
    page: Page
    dashboard_url: str | None = Field(
        None, description="The queue view with these filters already applied."
    )
    suggestions: list[dict[str, Any]] = Field(description="At most `limit` rows, best first.")


class FindArticlesResult(BaseModel):
    site_id: int
    match_count: int = Field(
        description="Articles matching the filters in total, not the number returned."
    )
    page: Page
    articles: list[dict[str, Any]]


class SiteJobsResult(BaseModel):
    site_id: int
    match_count: int = Field(description="Job runs matching in total, not the number returned.")
    page: Page
    jobs: list[dict[str, Any]]


class SuggestionHistoryResult(BaseModel):
    trace_id: str | None = Field(
        None, description="The trace these events belong to, when the call named one."
    )
    match_count: int = Field(description="Events matching in total, not the number returned.")
    page: Page
    events: list[dict[str, Any]]


class IngestionDiagnosticsResult(BaseModel):
    run: dict[str, Any] = Field(description="The ingestion run these rows came from.")
    counts: dict[str, int] = Field(
        description="The run's own URL counters: discovered, accepted, skipped, upserted, links."
    )
    reasons: dict[str, int] = Field(
        description="The crawl's own histogram: reason code to number of URLs. Complete."
    )
    match_count: int = Field(
        description="Diagnostic rows matching the reason filter in total; `examples` is a sample."
    )
    examples: list[dict[str, Any]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Any]
    #: Human-readable label. MCP clients show it in place of the snake_case
    #: name in permission prompts and call traces; the model still calls
    #: ``name``. Falls back to the name when unset.
    title: str | None = None
    #: The shape this tool answers with, published to MCP clients as
    #: ``outputSchema``. Optional on purpose: a tool whose payload is a handful
    #: of unambiguous scalars gains nothing from a contract, and an unused model
    #: is one more thing to keep true. Declared shapes are kept true by
    #: ``test_declared_output_schemas_describe_the_real_payload``.
    output_model: type[BaseModel] | None = None
    #: Fleet-wide surfaces (evaluation) are admin-only for scoped keys on the
    # REST API too; the registry enforces the same line so neither surface can
    # forget it.
    admin_only: bool = False


def _dashboard_url(path: str, **params: Any) -> str | None:
    """A link into the dashboard, or ``None`` when this deployment has no base.

    Tool results are ids, which is enough inside the dashboard — the operator
    is already there. Over MCP it is a dead end: the whole point is that a
    person reads the answer in their editor and then goes and reviews. The
    queue keeps its filters in the URL deliberately (see the frontend's
    ``useQueueFilters``), so a link can carry the exact view being described.

    A base that is not an absolute http(s) URL is treated as unset. A wrong
    link is worse than no link: the operator cannot tell a typo in deployment
    config from a bug in the engine, and follows it either way.
    """
    base = settings.dashboard_base_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    query = {key: str(value) for key, value in params.items() if value is not None and value != ""}
    return f"{base}{path}?{urlencode(query)}" if query else f"{base}{path}"


def _queue_url(
    *,
    site_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    target_origin: str | None = None,
    exclude_reciprocal: bool = False,
    min_percent: int | None = None,
    threshold: int | None = None,
) -> str | None:
    """The review queue showing exactly one set of filters.

    Parameter names are the frontend's, not this module's: ``site``, ``origin``,
    ``unique``, ``min``. They are read back by ``useQueueFilters``, which falls
    back to a default for anything it does not recognise — so a link that drifts
    degrades to a wider queue rather than an error page.
    """
    return _dashboard_url(
        "/queue",
        site=site_id,
        status=status,
        q=q,
        origin=target_origin,
        unique="1" if exclude_reciprocal else None,
        min=min_percent,
        threshold=threshold,
    )


def _urls(**candidates: str | None) -> dict[str, str]:
    """Only the links this deployment can actually build.

    With no ``dashboard_base_url`` every builder returns ``None``, and a
    payload full of null links reads to a model as "there is no link for this",
    which it then says out loud. Absent keys say nothing at all.
    """
    return {key: value for key, value in candidates.items() if value}


def _compact_site(site_out: Any, *, active_suggestion_count: int) -> dict[str, Any]:
    """One site's row, grouped so a count cannot be read as a capacity.

    ``slots_available`` is room left, not a number of suggestions. Flat beside
    ``active_suggestion_count`` the two read as synonyms, and a site at
    capacity publishes both 0 and 147 at the same level with nothing to tell
    them apart — either is a plausible answer to "how many suggestions do I
    have", and the wrong one is the kind of figure nobody double-checks.
    Separate nouns are what make the answer unambiguous, which is why
    ``get_site_status`` already shapes its payload this way.

    The counts are the same active figures ``GET /sites`` returns, so this
    view and the Sites page cannot disagree.
    """
    slots_available = site_out.suggestion_slots_available
    return {
        "id": site_out.id,
        "name": site_out.name,
        "base_url": site_out.base_url,
        "platform": site_out.platform,
        "content": {
            "active_article_count": site_out.article_count,
            "active_internal_link_count": site_out.internal_link_count,
        },
        "queue": {"active_suggestion_count": active_suggestion_count},
        "suggestion_capacity": {
            "slots_available": slots_available,
            "at_capacity": slots_available == 0,
        },
        "last_ingestion_status": site_out.last_ingestion_status,
        "has_wordpress_credentials": site_out.has_wordpress_credentials,
        **_urls(queue_url=_queue_url(site_id=site_out.id, status="pending")),
    }


def _sites(db: Session, principal: Principal, args: ListSitesArgs) -> dict[str, Any]:
    rows = list_sites(limit=50, offset=0, search=args.search, principal=principal, db=db)
    _, _, active_suggestion_counts = _site_counts(db, [row.id for row in rows])
    return {
        "sites": [
            _compact_site(
                row,
                active_suggestion_count=active_suggestion_counts.get(row.id, 0),
            )
            for row in rows
        ]
    }


def _site_status(db: Session, principal: Principal, args: SiteStatusArgs) -> dict[str, Any]:
    """One site in depth, and why its suggestion capacity is where it is.

    ``list_sites`` reports ``slots_available`` as a bare number, so a site
    sitting at 0 gives an operator no way to ask why — the ceiling is two
    settings and an article count that appear nowhere in the API. This tool
    shows the arithmetic instead of the result, and names which of the two
    limits is actually binding.
    """
    site = authorize_site_read(db, principal, _resolve_site_id(db, principal, args.site_id))
    out = get_site(site=site, db=db)
    _, _, active_counts = _site_counts(db, [site.id])
    active = active_counts.get(site.id, 0)
    counts = count_suggestions(
        site_id=site.id,
        method=None,
        min_percent=None,
        max_percent=None,
        q=None,
        target_origin=None,
        exclude_reciprocal=False,
        principal=principal,
        db=db,
    ).model_dump(mode="json")

    per_article_capacity = out.article_count * settings.hybrid_max_suggestions_per_article
    site_capacity = settings.hybrid_max_active_suggestions_per_site
    is_pool = site.platform == POOL_PLATFORM
    if is_pool:
        binding = "pool_source"
    elif per_article_capacity <= site_capacity:
        binding = "per_article"
    else:
        binding = "per_site"

    capacity: dict[str, Any] = {
        # Read back from the route rather than recomputed, so the number here
        # and the number on the Sites page cannot disagree. The breakdown below
        # explains it; it is not a second source of truth for it.
        "slots_available": out.suggestion_slots_available,
        "active_suggestion_count": active,
        "at_capacity": out.suggestion_slots_available == 0,
        "binding_limit": binding,
        "limits": {
            "per_article": {
                "cap_per_article": settings.hybrid_max_suggestions_per_article,
                "active_article_count": out.article_count,
                "capacity": per_article_capacity,
            },
            "per_site": {"capacity": site_capacity},
        },
        # The active set is a status set, which is what makes the two ways out
        # of a full queue derivable: a rejected row leaves it, and so does a
        # published one, but an approved row does not.
        "statuses_counting_toward_capacity": ["pending", "approved", "applying"],
        "slots_freed_by": {
            "rejecting_all_pending": counts.get("pending", 0),
            "publishing_all_approved": counts.get("approved", 0),
        },
    }
    if binding == "per_article":
        capacity["capacity_per_additional_article"] = settings.hybrid_max_suggestions_per_article
    if is_pool:
        # A pool source is a link target for other tenants, never a site whose
        # own articles get suggestions, so its ceiling is zero by definition.
        capacity["note"] = "content-pool sources never receive suggestions of their own"

    return {
        **_urls(
            sites_url=_dashboard_url("/sites", q=site.name),
            queue_url=_queue_url(site_id=site.id, status="pending"),
            publication_url=_dashboard_url(f"/publish/{site.id}"),
        ),
        "site": {
            "id": site.id,
            "name": site.name,
            "base_url": site.base_url,
            "platform": site.platform,
            "crawl_frequency": out.crawl_frequency,
            "suggestion_method": out.suggestion_method,
            "has_wordpress_credentials": out.has_wordpress_credentials,
        },
        "content": {
            "active_article_count": out.article_count,
            "active_internal_link_count": out.internal_link_count,
        },
        "ingestion": {
            "last_status": out.last_ingestion_status,
            "last_crawl_at": out.last_crawl_at.isoformat() if out.last_crawl_at else None,
        },
        "queue": counts,
        "suggestion_capacity": capacity,
    }


def _queue_counts(db: Session, principal: Principal, args: QueueCountsArgs) -> dict[str, Any]:
    # Every route parameter is passed explicitly: when a function is called
    # directly, FastAPI's Query(...) defaults are not resolved, so omitting one
    # would hand the raw Query descriptor to the query logic.
    counts = count_suggestions(
        site_id=args.site_id,
        method=args.method,
        min_percent=None,
        max_percent=None,
        q=args.q,
        target_origin=None,
        exclude_reciprocal=False,
        principal=principal,
        db=db,
    )
    return counts.model_dump(mode="json")


def _parse_cursor(cursor: str) -> tuple[float, int]:
    """Read a ``rank_score:id`` continuation token.

    The route takes the two sort keys as separate parameters that are only
    valid together — a pairing a model gets wrong half the time. One opaque
    string it copies back verbatim cannot be half-supplied.
    """
    rank_text, _, id_text = cursor.partition(":")
    try:
        rank_score, suggestion_id = float(rank_text), int(id_text)
    except ValueError:
        raise HTTPException(
            422, f"malformed cursor {cursor!r}; pass next_cursor unchanged"
        ) from None
    if not 0 <= rank_score <= 1 or suggestion_id < 1:
        raise HTTPException(422, f"cursor {cursor!r} is out of range")
    return rank_score, suggestion_id


def _search_queue(db: Session, principal: Principal, args: SearchQueueArgs) -> dict[str, Any]:
    after_rank_score, after_id = _parse_cursor(args.cursor) if args.cursor else (None, None)
    page = list_suggestion_page(
        site_id=args.site_id,
        status=args.status,
        method=args.method,
        min_percent=args.min_percent,
        max_percent=args.max_percent,
        q=args.q,
        target_origin=args.target_origin,
        exclude_reciprocal=args.exclude_reciprocal,
        after_rank_score=after_rank_score,
        after_id=after_id,
        # Count the whole match once, on the first page. Continuations ride the
        # route's look-ahead row instead, which is what the cursor is for.
        include_total=args.cursor is None,
        limit=args.limit,
        offset=None,
        principal=principal,
        db=db,
    )
    items = [
        {
            "id": item.id,
            "trace_id": item.trace_id,
            "site_id": item.site_id,
            "status": item.status,
            "method": item.method,
            # Two different questions, so two fields. `rank_percent` is what
            # ordered this page and what the dashboard card shows;
            # `similarity_percent` is cosine, which on a real corpus barely
            # varies and must not be mistaken for the ranking.
            "rank_percent": round(item.rank_score * 100),
            "score": round(item.score, 4),
            "similarity_percent": round(item.score * 100),
            "source_title": item.source_article.title,
            "target_title": item.target_article.title if item.target_article else None,
            "target_origin": item.target_origin,
        }
        for item in page.items
    ]
    # Field order is load-bearing. The transcript budget trims from the end, so
    # anything after `suggestions` is what gets dropped first — and `total` and
    # the cursor sitting there is how a 50-row page answered "how many match"
    # with its own page size and called the list complete. Decisive scalars
    # lead; the rows are last because they are the part that may be shortened.
    #
    # `page.returned` is nested rather than left beside the count for the same
    # reason `list_sites` groups a capacity away from a count: two bare
    # integers at one level are two plausible answers to one question.
    result: dict[str, Any] = {}
    if page.total is not None:
        # Issued on the first page only; continuations ride the look-ahead row.
        result["match_count"] = page.total
    result["page"] = {"has_more": page.next_cursor is not None}
    if page.next_cursor is not None:
        result["page"]["next_cursor"] = f"{page.next_cursor.rank_score}:{page.next_cursor.id}"
    # The view these rows came from, so the answer ends somewhere the operator
    # can act rather than at a list of ids.
    result.update(
        _urls(
            dashboard_url=_queue_url(
                site_id=args.site_id,
                status=args.status,
                q=args.q,
                target_origin=args.target_origin,
                exclude_reciprocal=args.exclude_reciprocal,
                min_percent=args.min_percent,
            )
        )
    )
    result["suggestions"] = items
    return result


def _parse_when(label: str, value: str | None) -> datetime | None:
    """Read a date or datetime an agent supplied, as UTC.

    Two accommodations for how models actually write dates. A bare
    ``2026-08-01`` is the common case and is read as midnight UTC rather than
    refused for having no timezone, and anything unparseable comes back as a
    422 naming the field — the plain ``ValueError`` reached ``call_tool``'s
    catch-all and was reported as a 500, which reads as "the server is broken,
    try again" to a model rather than "fix your argument".
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            422, f"{label} must be an ISO-8601 date or datetime, e.g. 2026-08-01"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _evaluation_metrics(db: Session, principal: Principal, args: EvaluationArgs) -> dict[str, Any]:
    out = get_evaluation_metrics(
        site_id=args.site_id,
        date_from=_parse_when("date_from", args.date_from),
        date_to=_parse_when("date_to", args.date_to),
        db=db,
    )
    return out.model_dump(mode="json")


#: Publication kinds, so a site's history shows the runs that moved its links
#: rather than every crawl and analysis it has ever done.
PUBLICATION_JOB_KINDS = ("publication", "publication_preparation")


def _prepared_plan_counts(db: Session, site_ids: list[int]) -> dict[int, int]:
    """Plans prepared but not yet approved, per site.

    ``publication_status`` counts only approved plans, so a plan sitting
    prepared is invisible to it — and "prepare" is then reported as the next
    action for a site whose real next step is approving what it already has.
    """
    if not site_ids:
        return {}
    rows = db.execute(
        select(PublicationPlan.site_id, func.count())
        .where(PublicationPlan.site_id.in_(site_ids), PublicationPlan.status == "prepared")
        .group_by(PublicationPlan.site_id)
    ).all()
    return dict(rows)


def _publication_next_action(
    *,
    selected_suggestions: int,
    approved_plans: int,
    prepared_plans: int,
    can_publish: bool,
    can_export: bool,
) -> tuple[str, str]:
    """What is actually blocking this site, as an action and one line of why.

    The two counts mean different things and ask for different work — the
    schema note on ``PendingPublicationSite`` is the authority. Selected
    suggestions are editorial intent that still needs preparing and approving;
    approved plans are artifacts a named human is already bound to, and are the
    only things a publication job may queue. Ordered by what has to happen
    first, so the answer is the *next* step rather than a list of everything.
    """
    if not can_publish and not can_export:
        return (
            "blocked",
            "no WordPress account is attached, so preparation would fail on every article",
        )
    if approved_plans:
        return (
            "publish",
            f"{approved_plans} approved plan(s) are ready to queue",
        )
    if prepared_plans:
        # One approval away from publishable, so it outranks preparing more.
        return (
            "approve_plan",
            f"{prepared_plans} prepared plan(s) are waiting for a human to approve them",
        )
    if selected_suggestions:
        return (
            "prepare",
            f"{selected_suggestions} approved suggestion(s) still need a prepared, approved plan",
        )
    return ("nothing_waiting", "no approved suggestions and no approved plans")


def _next_action_fields(**kwargs: Any) -> dict[str, str]:
    """Both halves of the ranking, so the fleet view explains itself too.

    The fleet rows used to take the action and drop the reason, which meant
    calling this twice per site would have been needed to report both — and
    left the two views disagreeing about how much they explain.
    """
    action, detail = _publication_next_action(**kwargs)
    return {"next_action": action, "next_action_detail": detail}


def _publication_status(
    db: Session, principal: Principal, args: PublicationStatusArgs
) -> dict[str, Any]:
    """Publication state: one site in depth, or every site with work waiting.

    Deliberately does not reuse ``pending_publication_site``, which 404s when a
    site has nothing waiting. "Nothing is waiting" is an answer to this
    question, not a failure of it.
    """
    if args.site_id is None:
        page = pending_publication_sites(
            limit=args.limit,
            cursor=None,
            search=None,
            include_totals=True,
            principal=principal,
            db=db,
        )
        # Enriches the rows the route returns; it does not change which rows
        # those are. A site whose *only* work is a prepared plan is outside
        # `_pending_publication_query`'s definition of pending and stays
        # outside it here, so this view and the dashboard's inbox agree.
        prepared = _prepared_plan_counts(db, [row.site_id for row in page.items])
        return {
            "totals": {
                "sites_with_work": page.total_sites,
                "selected_suggestions": page.total_selected_suggestions,
                "approved_plans": page.total_approved_plans,
            },
            "sites": [
                {
                    "site_id": row.site_id,
                    "site_name": row.site_name,
                    "platform": row.platform,
                    "selected_suggestions": row.selected_suggestions,
                    "approved_plans": row.approved_plans,
                    "prepared_plans": prepared.get(row.site_id, 0),
                    "can_publish": row.can_publish,
                    "can_export": row.can_export,
                    **_next_action_fields(
                        selected_suggestions=row.selected_suggestions,
                        approved_plans=row.approved_plans,
                        prepared_plans=prepared.get(row.site_id, 0),
                        can_publish=row.can_publish,
                        can_export=row.can_export,
                    ),
                    **_urls(publication_url=_dashboard_url(f"/publish/{row.site_id}")),
                }
                for row in page.items
            ],
        }

    # Same dependency the REST route uses: publication is an operational
    # surface, so this is authorize_site, not the wider read.
    site = authorize_site(db, principal, args.site_id)
    counts = publication_status(site=site, db=db)
    can_publish = site.platform == "wordpress" and site.has_wordpress_credentials
    can_export = site.platform == "html"
    prepared_plans = _prepared_plan_counts(db, [site.id]).get(site.id, 0)
    next_action = _next_action_fields(
        selected_suggestions=counts["selected_suggestions"],
        approved_plans=counts["approved_plans"],
        prepared_plans=prepared_plans,
        can_publish=can_publish,
        can_export=can_export,
    )
    # Queried directly rather than through ``list_job_runs``, which takes one
    # kind and this needs two. Fetching 50 rows of every kind and filtering in
    # Python lost the publication jobs entirely on a site whose crawls and
    # analyses are more recent — and an empty list here reads as "publication
    # has never run", which is a different answer.
    runs = db.scalars(
        select(JobRun)
        .where(JobRun.site_id == site.id, JobRun.kind.in_(PUBLICATION_JOB_KINDS))
        .order_by(JobRun.enqueued_at.desc())
        .limit(5)
    ).all()
    return {
        "site_id": site.id,
        "site_name": site.name,
        "platform": site.platform,
        "applied": counts["applied"],
        # Kept apart because they ask for different work — see next_action.
        "selected_suggestions": counts["selected_suggestions"],
        "approved_plans": counts["approved_plans"],
        # Not part of `publication_status`, which counts only approved plans.
        "prepared_plans": prepared_plans,
        "can_publish": can_publish,
        "can_export": can_export,
        **next_action,
        "recent_publication_jobs": [
            {
                "id": run.id,
                "kind": run.kind,
                "status": run.status,
                "enqueued_at": run.enqueued_at.isoformat(),
                "error": _clip(run.error, 300),
            }
            for run in runs
        ],
        **_urls(publication_url=_dashboard_url(f"/publish/{site.id}")),
    }


def _suggestion_history(
    db: Session, principal: Principal, args: SuggestionHistoryArgs
) -> dict[str, Any]:
    """The audit trail behind one suggestion, or a site's recent activity.

    ``explain_suggestion`` reports what a suggestion is now. This reports how
    it got there: who acted, when, and what each step changed.
    """
    trace_id = args.trace_id
    if trace_id is None and args.suggestion_id is not None:
        suggestion = db.get(Suggestion, args.suggestion_id)
        if suggestion is None:
            raise HTTPException(404, f"suggestion {args.suggestion_id} not found")
        authorize_site(db, principal, suggestion.site_id)
        trace_id = suggestion.trace_id

    page = list_trace_events(
        trace_id=trace_id,
        actor=None,
        event_type=args.event_type,
        status=None,
        site_id=args.site_id,
        date_from=None,
        date_to=None,
        limit=args.limit,
        offset=args.offset,
        principal=principal,
        db=db,
    )
    return {
        "trace_id": trace_id,
        "match_count": page.total,
        "page": {
            "offset": page.offset,
            "has_more": page.offset + len(page.items) < page.total,
        },
        # Last: the only key here whose rows a tight transcript budget may
        # shorten, so the counts above it always survive.
        "events": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "actor": item.actor,
                "created_at": item.created_at.isoformat(),
                "site_id": item.site_id,
                "trace_id": item.trace_id,
                "source_title": item.source_title,
                "target_title": item.target_title,
                "suggestion_status": item.suggestion_status,
                "publish_error": _clip(item.publish_error, 200),
            }
            for item in page.items
        ],
    }


def _ingestion_diagnostics(
    db: Session, principal: Principal, args: IngestionDiagnosticsArgs
) -> dict[str, Any]:
    """Why a crawl found what it found.

    Leads with the run's own ``diagnostic_summary`` — a reason-code histogram
    the crawl already computed — because "why did this crawl only keep 12
    pages" is answered by counts per reason, not by a list of URLs. Examples
    come second, and few of them: the whole table can run to thousands of rows.
    """
    site = authorize_site(db, principal, _resolve_site_id(db, principal, args.site_id))
    if args.run_id is None:
        run = latest_ingestion_run(site=site, db=db)
    else:
        run = db.scalars(
            select(IngestionRun).where(
                IngestionRun.id == args.run_id, IngestionRun.site_id == site.id
            )
        ).first()
        if run is None:
            raise HTTPException(404, f"ingestion run {args.run_id} not found")

    # The route has no reason filter, and applying one in Python to its first
    # 200 rows reported zero examples for a reason whose rows sat outside that
    # window — while the histogram beside it counted thousands. The filter and
    # the count both belong in SQL; the composite index
    # (ingestion_run_id, reason_code) is there for exactly this.
    conditions = [IngestionDiagnostic.ingestion_run_id == run.id]
    if args.reason_code:
        conditions.append(IngestionDiagnostic.reason_code == args.reason_code)
    match_count = (
        db.scalar(select(func.count()).select_from(IngestionDiagnostic).where(*conditions)) or 0
    )
    rows = (
        db.scalars(
            select(IngestionDiagnostic)
            .where(*conditions)
            .order_by(IngestionDiagnostic.id)
            .limit(args.examples)
        ).all()
        if args.examples
        else []
    )

    return {
        "run": {
            "id": run.id,
            "site_id": run.site_id,
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": _clip(run.error, 300),
        },
        "counts": {
            "discovered_urls": run.discovered_urls,
            "accepted_urls": run.accepted_urls,
            "skipped_urls": run.skipped_urls,
            "articles_upserted": run.articles_upserted,
            "links_found": run.links_found,
        },
        # The crawl's own histogram: reason code to number of URLs.
        "reasons": run.diagnostic_summary or {},
        # How many diagnostic rows the filter matches in total. The examples
        # below are a sample of them, never the answer to "how many".
        "match_count": match_count,
        "examples": [
            {
                "url": row.url,
                "state": row.state,
                "reason_code": row.reason_code,
                "reason_detail": _clip(row.reason_detail, 200),
                "discovered_from": row.discovered_from,
            }
            for row in rows
        ],
    }


def _site_jobs(db: Session, principal: Principal, args: SiteJobsArgs) -> dict[str, Any]:
    """One site's recent jobs, finished ones included.

    ``list_active_jobs`` shows only what is queued or running, so a job that
    failed ten minutes ago is invisible there.
    """
    site = authorize_site(db, principal, _resolve_site_id(db, principal, args.site_id))
    # Rows stay the route's, so ordering cannot drift; the count is this tool's
    # because the route has none and a capped list without one reads complete.
    conditions = [JobRun.site_id == site.id]
    if args.kind:
        conditions.append(JobRun.kind == args.kind)
    match_count = db.scalar(select(func.count()).select_from(JobRun).where(*conditions)) or 0
    runs = list_job_runs(site=site, kind=args.kind, limit=args.limit, offset=0, db=db)
    return {
        "site_id": site.id,
        "match_count": match_count,
        "page": {"has_more": match_count > len(runs)},
        "jobs": [
            {
                "id": run.id,
                "kind": run.kind,
                "status": run.status,
                "attempts": run.attempts,
                "requested_by": run.requested_by,
                "enqueued_at": run.enqueued_at.isoformat(),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "error": _clip(run.error, 300),
            }
            for run in runs
        ],
    }


def _graph_summary(db: Session, principal: Principal, args: GraphSummaryArgs) -> dict[str, Any]:
    site = authorize_site(db, principal, _resolve_site_id(db, principal, args.site_id))
    from app.api.routes.graph import get_graph_summary

    summary = get_graph_summary(site=site, limit=20, offset=0, db=db)
    return summary.model_dump(mode="json")


def _active_jobs(db: Session, principal: Principal, args: ActiveJobsArgs) -> dict[str, Any]:
    rows = list_active_job_runs(limit=args.limit, principal=principal, db=db)
    jobs = [
        {
            "id": row.id,
            "site_id": row.site_id,
            "kind": row.kind,
            "status": row.status,
            "enqueued_at": row.enqueued_at.isoformat(),
            "error": row.error,
        }
        for row in rows
    ]
    return {"active_jobs": jobs}


def _find_articles(db: Session, principal: Principal, args: FindArticlesArgs) -> dict[str, Any]:
    """Same visibility rules as GET /sites/{id}/articles, plus a title filter.

    The route has no text search, so this query is its own — but access goes
    through ``authorize_site_read``, the exact dependency the route uses, so a
    tenant key sees precisely the sites it may already read.
    """
    site = authorize_site_read(db, principal, _resolve_site_id(db, principal, args.site_id))
    conditions = [Article.site_id == site.id, Article.is_active.is_(True)]
    if args.q:
        pattern = f"%{args.q.strip()}%"
        conditions.append(Article.title.ilike(pattern) | Article.url.ilike(pattern))
    if args.orphans:  # Expired links do not count (Phase 0, finding 3).
        conditions.append(
            ~exists().where(
                InternalLink.target_article_id == Article.id,
                InternalLink.is_active.is_(True),
            )
        )
    # Counted separately because the rows are capped. A bare `returned` after a
    # LIMIT is indistinguishable from a complete answer, and "how many articles
    # are orphans" is exactly the question asked of this tool.
    match_count = db.scalar(select(func.count()).select_from(Article).where(*conditions)) or 0
    rows = db.scalars(
        select(Article).where(*conditions).order_by(Article.id.desc()).limit(args.limit)
    ).all()
    return {
        "site_id": site.id,
        "match_count": match_count,
        "page": {"has_more": match_count > len(rows)},
        "articles": [
            {
                "id": row.id,
                "title": row.title,
                "url": row.url,
                "published_at": row.published_at.isoformat() if row.published_at else None,
            }
            for row in rows
        ],
    }


def _clip(text: str | None, limit: int = 1_800) -> str | None:
    """Bound article text so one explanation cannot eat the model's context."""
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def _explain_suggestion(
    db: Session, principal: Principal, args: ExplainSuggestionArgs
) -> dict[str, Any]:
    # Both articles are read below, so fetch them with the row rather than
    # letting two lazy loads turn one explanation into three round trips.
    suggestion = db.scalars(
        select(Suggestion)
        .options(joinedload(Suggestion.source_article), joinedload(Suggestion.target_article))
        .where(Suggestion.id == args.suggestion_id)
    ).first()
    if suggestion is None:
        raise HTTPException(404, f"suggestion {args.suggestion_id} not found")
    authorize_site(db, principal, suggestion.site_id)

    source = suggestion.source_article
    target = suggestion.target_article

    def article_brief(article: Article) -> dict[str, Any]:
        return {
            "id": article.id,
            "title": article.title,
            "url": article.url,
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "content_excerpt": _clip(article.content_text),
        }

    explanation: dict[str, Any] = {
        "id": suggestion.id,
        "trace_id": suggestion.trace_id,
        "site_id": suggestion.site_id,
        "status": suggestion.status,
        "method": suggestion.method,
        # Two separate readings, both in raw and percent form. The queue is
        # ordered by rank_score and the dashboard card shows rank_percent;
        # cosine is reported beside it, never in place of it.
        "rank_score": round(suggestion.rank_score, 4),
        "rank_percent": round(suggestion.rank_score * 100),
        "score": round(suggestion.score, 4),
        "similarity_percent": round(suggestion.score * 100),
        "score_components": suggestion.score_components,
        "anchor_text": suggestion.anchor_text,
        "placement_context": _clip(suggestion.placement_context, 600),
        "placement_generated_at": (
            suggestion.placement_generated_at.isoformat()
            if suggestion.placement_generated_at
            else None
        ),
        "reviewer_id": suggestion.reviewer_id,
        "rejection_reason": suggestion.rejection_reason,
        "publish_outcome": suggestion.publish_outcome,
        "publish_attempts": suggestion.publish_attempts,
        # Clipped like every other reading of this column: a WordPress failure
        # body is the long one, and this is the tool most likely to meet it.
        "publish_error": _clip(suggestion.publish_error, 300),
        "ranking": {
            "retrieval_version": suggestion.retrieval_version,
            "ranking_version": suggestion.ranking_version,
            "final_rank": suggestion.final_rank,
            "rank_score": round(suggestion.rank_score, 4),
        },
        "source_article": article_brief(source),
        **_urls(
            dashboard_url=_queue_url(site_id=suggestion.site_id, status=suggestion.status),
            # Past review, the row lives on the publication page, which focuses
            # one suggestion by id — a precise link the queue cannot offer.
            publication_url=(
                _dashboard_url(f"/publish/{suggestion.site_id}", suggestion=suggestion.id)
                if suggestion.status in ("approved", "applying", "applied")
                else None
            ),
        ),
    }
    if target is not None:
        explanation["target_article"] = article_brief(target)
    else:
        explanation["external_target"] = {
            "url": suggestion.external_url,
            "title": suggestion.external_title,
            "snippet": suggestion.external_snippet,
            "provider": suggestion.provider,
            "provider_score": suggestion.provider_score,
            "search_query": suggestion.search_query,
        }
    return explanation


def _ops_digest(db: Session, principal: Principal, args: OpsDigestArgs) -> dict[str, Any]:
    owned = tenant_site_filter(principal)

    alert_query = select(Alert)
    if not args.include_acknowledged_alerts:
        alert_query = alert_query.where(Alert.acknowledged_at.is_(None))
    if owned is not None:
        alert_query = alert_query.join(Site, Site.id == Alert.site_id).where(owned)
    alerts = db.scalars(alert_query.order_by(Alert.last_seen_at.desc()).limit(15)).all()

    job_query = select(JobRun).where(JobRun.status == "failed")
    if owned is not None:
        job_query = job_query.join(Site, Site.id == JobRun.site_id).where(owned)
    failed_jobs = db.scalars(job_query.order_by(JobRun.enqueued_at.desc()).limit(10)).all()

    stuck_query = (
        select(Suggestion)
        .options(joinedload(Suggestion.source_article))
        .where(Suggestion.status == "failed")
    )
    if owned is not None:
        stuck_query = stuck_query.join(Site, Site.id == Suggestion.site_id).where(owned)
    stuck = db.scalars(stuck_query.order_by(Suggestion.id.desc()).limit(10)).all()

    crawl_query = select(IngestionRun).where(IngestionRun.status == "failed")
    if owned is not None:
        crawl_query = crawl_query.join(Site, Site.id == IngestionRun.site_id).where(owned)
    failed_crawls = db.scalars(crawl_query.order_by(IngestionRun.started_at.desc()).limit(5)).all()

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def total(query: Any) -> int:
        return db.scalar(select(func.count()).select_from(query.subquery())) or 0

    return {
        # Complete totals for each section. The lists below are the most recent
        # few of each, so "how many jobs failed" is answered from here — a
        # digest that shows ten rows and no count can only be read as ten.
        "counts": {
            "alerts": total(alert_query),
            "failed_jobs": total(job_query),
            "stuck_suggestions": total(stuck_query),
            "failed_crawls": total(crawl_query),
        },
        "alerts": [
            {
                "id": alert.id,
                "site_id": alert.site_id,
                "kind": alert.kind,
                "subject": alert.subject,
                "occurrences": alert.occurrences,
                "last_seen_at": iso(alert.last_seen_at),
            }
            for alert in alerts
        ],
        "failed_jobs": [
            {
                "id": job.id,
                "site_id": job.site_id,
                "kind": job.kind,
                "error": _clip(job.error, 300),
                "finished_at": iso(job.finished_at),
            }
            for job in failed_jobs
        ],
        "stuck_suggestions": [
            {
                "id": row.id,
                "site_id": row.site_id,
                "attempts": row.publish_attempts,
                "source_title": row.source_article.title,
                "error": _clip(row.publish_error, 200),
            }
            for row in stuck
        ],
        "failed_crawls": [
            {
                "id": run.id,
                "site_id": run.site_id,
                "started_at": iso(run.started_at),
                "error": _clip(run.error, 300),
            }
            for run in failed_crawls
        ],
    }


#: Rows shown beside a bulk-rule preview. Enough to eyeball the match quality;
#: few enough that the operator still confirms a rule, not a list.
BULK_REVIEW_SAMPLE_ROWS = 3


def _preview_suggestion_review(
    db: Session, principal: Principal, args: PreviewSuggestionReviewArgs
) -> dict[str, Any]:
    """Stage one exact review decision without changing the suggestion.

    The proposal carries ``expected_status=pending``. If another editor acts
    before Confirm is clicked, the ordinary review endpoint returns 409 rather
    than silently replacing that newer decision.
    """
    suggestion = db.get(
        Suggestion,
        args.suggestion_id,
        options=[joinedload(Suggestion.source_article), joinedload(Suggestion.target_article)],
    )
    if suggestion is None:
        raise HTTPException(404, f"suggestion {args.suggestion_id} not found")
    authorize_site(db, principal, suggestion.site_id)
    if suggestion.status != "pending":
        raise HTTPException(
            409,
            f"suggestion {suggestion.id} is {suggestion.status}, not pending; refresh before acting",
        )

    status = "approved" if args.action == "approve" else "rejected"
    target_title = (
        suggestion.target_article.title
        if suggestion.target_article is not None
        else suggestion.external_title or suggestion.external_url
    )
    return {
        "action": args.action,
        "suggestion": {
            "id": suggestion.id,
            "site_id": suggestion.site_id,
            "current_status": suggestion.status,
            "rank_percent": round(suggestion.rank_score * 100),
            "source_title": suggestion.source_article.title,
            "target_title": target_title,
        },
        **_urls(review_url=_queue_url(site_id=suggestion.site_id, status="pending")),
        "proposal": {
            "kind": "review_suggestion",
            "risk": "reversible",
            "method": "PUT",
            "endpoint": f"/api/v1/suggestions/{suggestion.id}",
            "payload": {
                "status": status,
                "expected_status": "pending",
                "rejection_reason": args.rejection_reason,
            },
        },
    }


def _preview_bulk_review(
    db: Session, principal: Principal, args: PreviewBulkReviewArgs
) -> dict[str, Any]:
    """Count and sample a bulk rule. Never mutates: the confirm is human-only.

    The returned `proposal` mirrors `BulkReviewFilter` exactly — the dashboard
    posts it verbatim to `/api/v1/suggestions/bulk-review-by-filter` when (and
    only when) the operator clicks Confirm, so execution rides the audited,
    undoable REST path rather than any agent-specific code.
    """
    # Shape is the args model's job now; this is the part that needs a caller.
    if args.all_sites:
        require_admin_principal(principal)
    else:
        authorize_site(db, principal, args.site_id)

    conditions = _queue_conditions(
        site_id=args.site_id,
        tenant_id=_tenant_scope(principal),
        status="pending",
        method=args.method,
        min_percent=args.threshold_percent if args.action == "approve" else None,
        max_percent=args.threshold_percent if args.action == "reject" else None,
        q=args.q,
        target_origin=args.target_origin,
        exclude_reciprocal=args.exclude_reciprocal,
    )
    match_count = db.scalar(select(func.count()).select_from(Suggestion).where(*conditions)) or 0
    sample_rows = db.scalars(
        select(Suggestion)
        .options(joinedload(Suggestion.source_article), joinedload(Suggestion.target_article))
        .where(*conditions)
        .order_by(Suggestion.rank_score.desc(), Suggestion.id.desc())
        .limit(BULK_REVIEW_SAMPLE_ROWS)
    ).all()
    sample = [
        {
            "id": row.id,
            "rank_percent": round(row.rank_score * 100),
            "similarity_percent": round(row.score * 100),
            "source_title": row.source_article.title,
            "target_title": (
                row.target_article.title if row.target_article else row.external_title
            ),
        }
        for row in sample_rows
    ]
    # The wire field is BulkReviewFilter.status, whose vocabulary is the
    # past-tense status ("approved"/"rejected"), not the action verb.
    payload = {
        "status": "approved" if args.action == "approve" else "rejected",
        "match_status": "pending",
        "site_id": args.site_id,
        "all_sites": args.all_sites,
        "method": args.method,
        "threshold_percent": args.threshold_percent,
        "q": args.q,
        "target_origin": args.target_origin,
        "exclude_reciprocal": args.exclude_reciprocal,
        "rejection_reason": args.rejection_reason,
    }
    return {
        "action": args.action,
        "match_count": match_count,
        "sample": sample,
        # The dashboard's own confirm affordance, pre-filtered to this rule.
        # `proposal` below is the payload the panel posts; this is the link an
        # MCP client can hand a human instead, since it has no Confirm button.
        **_urls(
            review_url=_queue_url(
                site_id=args.site_id,
                status="pending",
                q=args.q,
                target_origin=args.target_origin,
                exclude_reciprocal=args.exclude_reciprocal,
                threshold=args.threshold_percent,
            )
        ),
        "proposal": {
            "kind": "bulk_review",
            "risk": "reversible",
            "method": "POST",
            "endpoint": "/api/v1/suggestions/bulk-review-by-filter",
            "payload": payload,
        },
    }


REGISTRY: dict[str, AgentTool] = {
    tool.name: tool
    for tool in [
        AgentTool(
            name="explain_suggestion",
            title="Explain suggestion",
            description=(
                "Full review context for one suggestion: both articles' text, the "
                "score explanation, placement passage, provenance, and publication "
                "history. Use before advising approve/reject."
            ),
            args_model=ExplainSuggestionArgs,
            handler=_explain_suggestion,
        ),
        AgentTool(
            name="preview_suggestion_review",
            title="Preview suggestion review",
            description=(
                "Stage approval or rejection of one pending suggestion after examining it with "
                "explain_suggestion. Returns an exact reversible proposal that the editor must "
                "confirm; this tool never changes the suggestion. Rejection requires a reason."
            ),
            args_model=PreviewSuggestionReviewArgs,
            handler=_preview_suggestion_review,
        ),
        AgentTool(
            name="get_ops_digest",
            title="Operational digest",
            description=(
                "Operational health in one call: recent alerts, failed jobs and "
                "crawls, and publication-stuck suggestions."
            ),
            args_model=OpsDigestArgs,
            handler=_ops_digest,
        ),
        AgentTool(
            name="preview_bulk_review",
            title="Preview bulk review",
            description=(
                "Count what a bulk rule WOULD review and show a sample. Returns a "
                "proposal the operator must confirm in the dashboard; this tool "
                "never changes anything. Scope: site_id, or all_sites=true (admin)."
            ),
            args_model=PreviewBulkReviewArgs,
            handler=_preview_bulk_review,
        ),
        AgentTool(
            name="list_sites",
            title="List sites",
            description=(
                "List connected sites with the canonical dashboard counts, grouped as "
                "content (articles, internal links), queue (suggestions), and "
                "suggestion_capacity (room left). Start here when the caller does not "
                "name a specific site."
            ),
            args_model=ListSitesArgs,
            handler=_sites,
        ),
        AgentTool(
            name="get_site_status",
            title="Site status",
            description=(
                "One site in depth: content and link counts, last crawl, per-status queue "
                "counts, and the suggestion-capacity arithmetic — the two caps, which one "
                "is binding, and how many slots rejecting or publishing would free. Use "
                "this when a site is at capacity, or when asked why a site is not "
                "producing new suggestions."
            ),
            args_model=SiteStatusArgs,
            handler=_site_status,
        ),
        AgentTool(
            name="get_queue_counts",
            title="Queue counts",
            description="Per-status suggestion counts for the review queue, optionally per site.",
            args_model=QueueCountsArgs,
            handler=_queue_counts,
        ),
        AgentTool(
            name="search_queue",
            title="Search review queue",
            description=(
                "Search review-queue suggestions, highest rank score first. Filter by "
                "site, status, text, method, target origin, and a rank-score band "
                "(min_percent/max_percent) — use the band to size a threshold before "
                "calling preview_bulk_review. `match_count` is how many rows match the "
                'filters in total; answer "how many" from it, never from the length of '
                "`suggestions`, which holds at most `limit` rows. To read the rest, pass "
                "`page.next_cursor` back as `cursor` with the same filters."
            ),
            args_model=SearchQueueArgs,
            output_model=SearchQueueResult,
            handler=_search_queue,
        ),
        AgentTool(
            name="get_evaluation_metrics",
            title="Evaluation metrics",
            description=(
                "Fleet-wide editorial, placement, publication, exposure, and method metrics "
                "over a generated-suggestion cohort. Admin only."
            ),
            args_model=EvaluationArgs,
            handler=_evaluation_metrics,
            admin_only=True,
        ),
        AgentTool(
            name="get_publication_status",
            title="Publication status",
            description=(
                "Publication state for one site, or every site with work waiting when "
                "site_id is omitted. Separates approved suggestions still needing a "
                "prepared plan from approved plans ready to queue, reports whether "
                "credentials allow publishing at all, and names the next action. Use "
                'for "what is blocking publication" and "what is ready to publish".'
            ),
            args_model=PublicationStatusArgs,
            handler=_publication_status,
        ),
        AgentTool(
            name="get_suggestion_history",
            title="Suggestion history",
            description=(
                "The audit trail for one suggestion — who acted, when, and what changed "
                "— by suggestion_id or trace_id. explain_suggestion says what a row is "
                "now; this says how it got there. Omit both ids and pass site_id for a "
                "site's recent review activity."
            ),
            args_model=SuggestionHistoryArgs,
            output_model=SuggestionHistoryResult,
            handler=_suggestion_history,
        ),
        AgentTool(
            name="get_ingestion_diagnostics",
            title="Crawl diagnostics",
            description=(
                "Why a crawl found what it found: the run's URL counts, a reason-code "
                "histogram, and example URLs. Defaults to the most recent run. Use for "
                '"why did the crawl only find N articles" or a failed crawl.'
            ),
            args_model=IngestionDiagnosticsArgs,
            output_model=IngestionDiagnosticsResult,
            handler=_ingestion_diagnostics,
        ),
        AgentTool(
            name="get_site_jobs",
            title="Site job history",
            description=(
                "Recent crawl, analysis, and publication jobs for one site, finished and "
                "failed ones included, with timings and errors. list_active_jobs shows "
                "only what is running now."
            ),
            args_model=SiteJobsArgs,
            output_model=SiteJobsResult,
            handler=_site_jobs,
        ),
        AgentTool(
            name="get_graph_summary",
            title="Link-graph summary",
            description=(
                "Structural link-graph observation for one site: orphan, underlinked, hub, "
                "and saturated article counts."
            ),
            args_model=GraphSummaryArgs,
            handler=_graph_summary,
        ),
        AgentTool(
            name="list_active_jobs",
            title="Active jobs",
            description="Crawl, analysis, and publication jobs currently queued or running.",
            args_model=ActiveJobsArgs,
            handler=_active_jobs,
        ),
        AgentTool(
            name="find_articles",
            title="Find articles",
            description=(
                "Find articles on a site by title/URL substring; can restrict to orphan pages."
            ),
            args_model=FindArticlesArgs,
            output_model=FindArticlesResult,
            handler=_find_articles,
        ),
    ]
}

#: Tools whose output is fleet-wide read the admin-only line the same way the
#: REST router does (see app/api/routes/evaluation.py for that decision).
ADMIN_ONLY_TOOLS = frozenset(name for name, tool in REGISTRY.items() if tool.admin_only)


def error_of(result: dict[str, Any]) -> str | None:
    """The failure message of a registry result, or ``None`` if it succeeded.

    Every handler failure leaves the ``{"error": str, "status": int}`` shape
    ``call_tool`` builds, and no successful payload carries both keys at the
    top level — nested rows (a failed job's ``error``) are one level down.
    Surfaces that distinguish failure from data (MCP's ``isError``) ask here
    rather than re-deriving the shape and drifting from it.
    """
    if isinstance(result.get("error"), str) and isinstance(result.get("status"), int):
        return f"{result['error']} (status {result['status']})"
    return None


def output_schema_violation(tool: AgentTool, result: dict[str, Any]) -> str | None:
    """Why ``result`` does not match what ``tool`` promised, or ``None``.

    fastmcp publishes ``outputSchema`` but does not enforce it, so a declared
    shape is a promise nothing checks. That is the failure mode this whole
    surface has been spending its time removing: a client reads the contract,
    trusts it, and is quietly wrong. A drifted payload is therefore reported
    here and logged.

    It is *not* turned into an error. A read-only status tool answering with an
    extra key is still a useful answer, and refusing it would turn a schema slip
    into an outage. Drift is meant to be caught by
    ``test_declared_output_schemas_describe_the_real_payload`` before it ships;
    this is the net under that, for a payload shape no fixture reaches.
    """
    if tool.output_model is None or error_of(result) is not None:
        return None
    try:
        tool.output_model.model_validate(result)
    except ValidationError as exc:
        return f"{tool.name} result does not match its output schema: {_argument_problems(exc)}"
    return None


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema without pydantic's ``title`` noise — models quote it back.

    Stripped at every depth, not just the top: an output model nests, and
    fastmcp inlines the nested definitions into what it publishes, so a title
    left on ``Page.returned`` reaches the client exactly like one left on the
    root would.
    """

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key != "title"}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(model.model_json_schema())


def openai_tool_specs() -> list[dict[str, Any]]:
    """The registry as chat-completions ``tools`` entries."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": json_schema(tool.args_model),
            },
        }
        for tool in REGISTRY.values()
    ]


#: How many field problems one rejection names. A model fixing its call needs
#: the fields, not all of them; the rest would only crowd the turn.
MAX_REPORTED_ARGUMENT_PROBLEMS = 5


def _resolve_site_id(db: Session, principal: Principal, site_id: int | None) -> int:
    """The site the caller means when they did not name one.

    "How many orphan articles do I have" names no site because, on a
    single-site account, there is only one to mean. Requiring the id anyway
    made a model spend rounds guessing one — we watched it invent 123, then
    burn the round cap retrying — and the operator got no answer to a question
    that had exactly one.

    With several sites this stays an error naming them, because choosing one
    would be a guess presented as an answer. Same line the rest of the module
    holds: answer exactly, or say what is needed.
    """
    if site_id is not None:
        return site_id
    rows = db.execute(
        select(Site.id, Site.name).where(*_owned_site_conditions(principal)).limit(10)
    ).all()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise HTTPException(404, "no sites are connected")
    named = ", ".join(f"{row_id} ({name})" for row_id, name in rows)
    raise HTTPException(422, f"name a site_id; you have several: {named}")


def _usable_site_hint(db: Session, principal: Principal) -> str:
    """The site ids this caller may use, appended to an error that named a bad one.

    A bare "site 123 not found" is a dead end: the model has to spend another
    round on ``list_sites`` to learn what it should have said, and a small model
    tends to retry the same wrong id instead. Naming the ids turns the refusal
    into the answer to the question the model was about to ask.

    Nothing is disclosed here that ``list_sites`` would not return to the same
    caller — the scope is the caller's own, and admins see the fleet either way.
    """
    rows = db.execute(
        select(Site.id, Site.name).where(*_owned_site_conditions(principal)).limit(10)
    ).all()
    if not rows:
        return ""
    named = ", ".join(f"{site_id} ({name})" for site_id, name in rows)
    return f" Sites you can use: {named}."


def _owned_site_conditions(principal: Principal) -> list[Any]:
    owned = tenant_site_filter(principal)
    return [] if owned is None else [owned]


def _argument_problems(exc: ValidationError) -> str:
    """Name what was wrong with an agent's arguments, and why.

    A bare problem count told a model nothing it could act on, so it retried
    the same call until the round cap ended the turn. Pydantic's messages are
    exactly what is needed here — a rejected Literal lists the values it does
    accept — and they describe the caller's own input, so there is nothing in
    them to withhold.
    """
    errors = exc.errors()
    named = [
        f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in errors[:MAX_REPORTED_ARGUMENT_PROBLEMS]
    ]
    if len(errors) > MAX_REPORTED_ARGUMENT_PROBLEMS:
        named.append(f"and {len(errors) - MAX_REPORTED_ARGUMENT_PROBLEMS} more")
    return "; ".join(named)


def call_tool(db: Session, principal: Principal, name: str, arguments: dict[str, Any]) -> dict:
    """Execute one registry tool, converting every failure into a payload.

    Authorization failures keep their REST semantics as data (403/404 with the
    same detail strings), so an agent can tell "no such site" from "not yours"
    exactly like a dashboard caller can.
    """
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}", "status": 404}
    if tool.admin_only and not principal.is_admin:
        return {"error": "admin access required for this tool", "status": 403}
    try:
        args = tool.args_model.model_validate(arguments or {})
    except ValidationError as exc:
        return {"error": f"invalid arguments: {_argument_problems(exc)}", "status": 422}
    try:
        result = tool.handler(db=db, principal=principal, args=args)
    except HTTPException as exc:
        detail = str(exc.detail)
        # A rejected site id is the one refusal an agent can act on immediately,
        # so it is answered with the ids that would have worked.
        if exc.status_code == 404 and detail.startswith("site "):
            detail += _usable_site_hint(db, principal)
        return {"error": detail, "status": exc.status_code}
    except Exception:  # noqa: BLE001 - surfaces stay answerable
        # Deliberately not `str(exc)`. A SQLAlchemy error stringifies to the
        # statement and its bound parameters, and over /mcp that reaches any
        # external client holding a key. The caller gets a fixed sentence; the
        # detail goes to the log, where an operator can read it.
        logger.exception("agent tool %r failed", name)
        return {"error": f"tool {name!r} failed unexpectedly", "status": 500}
    return result
