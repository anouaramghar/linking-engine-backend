"""The one read-only action registry behind both agent surfaces.

The MCP server (``app.mcp_server``) and the dashboard assistant
(``POST /api/v1/agent/chat``) both execute *these* handlers — there is no
second implementation anywhere. That is the whole point of the module: an
answer an agent gives is computed by exactly the code path the REST API uses,
including tenant scoping and site authorization, because most handlers below
call the route functions themselves rather than re-deriving their queries.

Every tool here reads. Nothing in this file may approve, reject, publish,
crawl, or enqueue: agents answer questions about work, they do not perform it.
The review workflow's success gates are human-only by design.

Handlers return plain JSON-safe dicts. Failures are returned as ``{"error":
..., "status": ...}`` rather than raised, because the chat loop reads outcomes
as data and a model copes better with a message it can quote than with an
exception. Surfaces that have a failure channel of their own translate on the
way out — see ``error_of`` and ``app.mcp_server``, which turns that shape into
an MCP ``isError`` result carrying the same text.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from typing import Any, Callable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.api.routes.evaluation import get_evaluation_metrics
from app.api.routes.ingestion import ingestion_diagnostics, latest_ingestion_run
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


class ListSitesArgs(BaseModel):
    search: str | None = Field(None, description="Filter by name, URL, or platform substring.")


class QueueCountsArgs(BaseModel):
    site_id: int | None = Field(None, description="Restrict to one site.")
    method: str | None = Field(None, description='Suggestion method filter, e.g. "hybrid_bm25".')
    q: str | None = Field(None, description="Title/URL text filter.")


class SearchQueueArgs(BaseModel):
    """The queue's filters, bounded for an agent.

    Every bound here is stated on the model rather than borrowed from the
    route: ``list_suggestion_page`` declares its limits with ``Query(...)``,
    and calling a route function directly leaves those descriptors unresolved,
    so nothing would validate ``min_percent`` or the length of ``q``.
    """

    site_id: int | None = Field(None, description="Restrict to one site.")
    status: str = Field(
        "pending",
        description="pending, approved, rejected, applying, applied, failed, or expired.",
    )
    q: str | None = Field(None, max_length=MAX_SEARCH_TERM, description="Title/URL text filter.")
    method: str | None = Field(None, description='Suggestion method filter, e.g. "hybrid_bm25".')
    min_percent: int | None = Field(
        None, ge=0, le=100, description="Only rows at or above this whole-percent similarity."
    )
    max_percent: int | None = Field(
        None, ge=0, le=100, description="Only rows below this whole-percent similarity."
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
    site_id: int


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
    event_type: str | None = Field(
        None, max_length=50, description="Restrict to one kind of event."
    )
    limit: int = Field(20, ge=1, le=50)
    offset: int = Field(0, ge=0)


class IngestionDiagnosticsArgs(BaseModel):
    site_id: int
    run_id: int | None = Field(None, description="Defaults to the most recent ingestion run.")
    reason_code: str | None = Field(
        None, max_length=80, description="Only example rows with this reason."
    )
    examples: int = Field(10, ge=0, le=25, description="Example URLs to return per call.")


class SiteJobsArgs(BaseModel):
    site_id: int
    kind: str | None = Field(
        None, description='One job kind, e.g. "ingestion", "analysis", "publication".'
    )
    limit: int = Field(15, ge=1, le=50)


class PublicationStatusArgs(BaseModel):
    site_id: int | None = Field(
        None,
        description=("One site's publication state. Omit for every site with work waiting."),
    )
    limit: int = Field(20, ge=1, le=50, description="Sites returned when site_id is omitted.")


class GraphSummaryArgs(BaseModel):
    site_id: int


class ActiveJobsArgs(BaseModel):
    limit: int = Field(15, ge=1, le=25)


class FindArticlesArgs(BaseModel):
    site_id: int
    q: str | None = Field(None, description="Title or URL substring to match.")
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
            "Whole-percent similarity boundary. approve = match pending rows at "
            "or above it; reject = match pending rows below it."
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
    method: str | None = None
    target_origin: Literal["internal", "content_pool", "web_search"] | None = None
    exclude_reciprocal: bool = False


class ExplainSuggestionArgs(BaseModel):
    suggestion_id: int


class OpsDigestArgs(BaseModel):
    include_acknowledged_alerts: bool = Field(
        False, description="Include alerts an operator already acknowledged."
    )


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
    return {
        "id": site_out.id,
        "name": site_out.name,
        "base_url": site_out.base_url,
        "platform": site_out.platform,
        # Keep the old short names for MCP clients that already consume them,
        # but expose the semantics explicitly to a language model. These are
        # the same active counts returned by GET /sites.
        "article_count": site_out.article_count,
        "internal_link_count": site_out.internal_link_count,
        "active_article_count": site_out.article_count,
        "active_internal_link_count": site_out.internal_link_count,
        "active_suggestion_count": active_suggestion_count,
        # This is capacity, not a count of suggestions. Naming it beside the
        # active count prevents the model from treating the two as synonyms.
        "suggestion_slots_available": site_out.suggestion_slots_available,
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

    ``list_sites`` reports ``suggestion_slots_available`` as a bare number, so
    a site sitting at 0 gives an operator no way to ask why — the ceiling is
    two settings and an article count that appear nowhere in the API. This
    tool shows the arithmetic instead of the result, and names which of the
    two limits is actually binding.
    """
    site = authorize_site_read(db, principal, args.site_id)
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
    """Read a ``score:id`` continuation token.

    The route takes the two sort keys as separate parameters that are only
    valid together — a pairing a model gets wrong half the time. One opaque
    string it copies back verbatim cannot be half-supplied.
    """
    score_text, _, id_text = cursor.partition(":")
    try:
        score, suggestion_id = float(score_text), int(id_text)
    except ValueError:
        raise HTTPException(
            422, f"malformed cursor {cursor!r}; pass next_cursor unchanged"
        ) from None
    if not 0 <= score <= 1 or suggestion_id < 1:
        raise HTTPException(422, f"cursor {cursor!r} is out of range")
    return score, suggestion_id


def _search_queue(db: Session, principal: Principal, args: SearchQueueArgs) -> dict[str, Any]:
    after_score, after_id = _parse_cursor(args.cursor) if args.cursor else (None, None)
    page = list_suggestion_page(
        site_id=args.site_id,
        status=args.status,
        method=args.method,
        min_percent=args.min_percent,
        max_percent=args.max_percent,
        q=args.q,
        target_origin=args.target_origin,
        exclude_reciprocal=args.exclude_reciprocal,
        after_score=after_score,
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
            "score": round(item.score, 4),
            "similarity_percent": round(item.score * 100),
            "source_title": item.source_article.title,
            "target_title": item.target_article.title if item.target_article else None,
            "target_origin": item.target_origin,
        }
        for item in page.items
    ]
    result: dict[str, Any] = {
        "returned": len(items),
        "suggestions": items,
        # The view these rows came from, so the answer ends somewhere the
        # operator can act rather than at a list of ids.
        **_urls(
            dashboard_url=_queue_url(
                site_id=args.site_id,
                status=args.status,
                q=args.q,
                target_origin=args.target_origin,
                exclude_reciprocal=args.exclude_reciprocal,
                min_percent=args.min_percent,
            )
        ),
    }
    if page.total is not None:
        result["total"] = page.total
    if page.next_cursor is not None:
        result["next_cursor"] = f"{page.next_cursor.score}:{page.next_cursor.id}"
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
                    "next_action": _publication_next_action(
                        selected_suggestions=row.selected_suggestions,
                        approved_plans=row.approved_plans,
                        prepared_plans=prepared.get(row.site_id, 0),
                        can_publish=row.can_publish,
                        can_export=row.can_export,
                    )[0],
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
    action, detail = _publication_next_action(
        selected_suggestions=counts["selected_suggestions"],
        approved_plans=counts["approved_plans"],
        prepared_plans=prepared_plans,
        can_publish=can_publish,
        can_export=can_export,
    )
    runs = [
        run
        for run in list_job_runs(site=site, kind=None, limit=50, offset=0, db=db)
        if run.kind in PUBLICATION_JOB_KINDS
    ][:5]
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
        "next_action": action,
        "next_action_detail": detail,
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
        "total": page.total,
        "returned": len(page.items),
        "offset": page.offset,
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
    site = authorize_site(db, principal, args.site_id)
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

    rows = (
        ingestion_diagnostics(run_id=run.id, site=site, limit=200, offset=0, db=db)
        if args.examples
        else []
    )
    if args.reason_code:
        rows = [row for row in rows if row.reason_code == args.reason_code]

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
        "examples": [
            {
                "url": row.url,
                "state": row.state,
                "reason_code": row.reason_code,
                "reason_detail": _clip(row.reason_detail, 200),
                "discovered_from": row.discovered_from,
            }
            for row in rows[: args.examples]
        ],
    }


def _site_jobs(db: Session, principal: Principal, args: SiteJobsArgs) -> dict[str, Any]:
    """One site's recent jobs, finished ones included.

    ``list_active_jobs`` shows only what is queued or running, so a job that
    failed ten minutes ago is invisible there.
    """
    site = authorize_site(db, principal, args.site_id)
    runs = list_job_runs(site=site, kind=args.kind, limit=args.limit, offset=0, db=db)
    return {
        "site_id": site.id,
        "returned": len(runs),
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
    site = authorize_site(db, principal, args.site_id)
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
    site = authorize_site_read(db, principal, args.site_id)
    query = select(Article).where(
        Article.site_id == site.id,
        Article.is_active.is_(True),
    )
    if args.q:
        pattern = f"%{args.q.strip()}%"
        query = query.where(Article.title.ilike(pattern) | Article.url.ilike(pattern))
    if args.orphans:  # Expired links do not count (Phase 0, finding 3).
        query = query.where(
            ~exists().where(
                InternalLink.target_article_id == Article.id,
                InternalLink.is_active.is_(True),
            )
        )
    rows = db.scalars(query.order_by(Article.id.desc()).limit(args.limit)).all()
    articles = [
        {
            "id": row.id,
            "title": row.title,
            "url": row.url,
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }
        for row in rows
    ]
    return {"site_id": site.id, "returned": len(articles), "articles": articles}


def _clip(text: str | None, limit: int = 1_800) -> str | None:
    """Bound article text so one explanation cannot eat the model's context."""
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def _explain_suggestion(
    db: Session, principal: Principal, args: ExplainSuggestionArgs
) -> dict[str, Any]:
    suggestion = db.get(Suggestion, args.suggestion_id)
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
        # The queue percentage is score*100; give the model both forms.
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
        "publish_error": suggestion.publish_error,
        "ranking": {
            "retrieval_version": suggestion.retrieval_version,
            "ranking_version": suggestion.ranking_version,
            "final_rank": suggestion.final_rank,
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

    return {
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


def _preview_bulk_review(
    db: Session, principal: Principal, args: PreviewBulkReviewArgs
) -> dict[str, Any]:
    """Count and sample a bulk rule. Never mutates: the confirm is human-only.

    The returned `proposal` mirrors `BulkReviewFilter` exactly — the dashboard
    posts it verbatim to `/api/v1/suggestions/bulk-review-by-filter` when (and
    only when) the operator clicks Confirm, so execution rides the audited,
    undoable REST path rather than any agent-specific code.
    """
    if args.all_sites:
        require_admin_principal(principal)
    elif args.site_id is None:
        return {"error": "set site_id or all_sites=true", "status": 422}
    if args.action == "reject" and args.rejection_reason is None:
        return {"error": "rejection_reason is required when rejecting", "status": 422}
    if args.rejection_reason is not None and args.action != "reject":
        return {"error": "rejection_reason is only valid when rejecting", "status": 422}
    if args.site_id is not None:
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
        .order_by(Suggestion.score.desc(), Suggestion.id.desc())
        .limit(BULK_REVIEW_SAMPLE_ROWS)
    ).all()
    sample = [
        {
            "id": row.id,
            "score_percent": round(row.score * 100),
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
                "List connected sites with canonical dashboard counts. Each site returns "
                "active_article_count, active_internal_link_count, and "
                "active_suggestion_count. suggestion_slots_available is remaining "
                "capacity, not a suggestion count. Start here when the caller does not "
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
                "this when a site shows 0 suggestion_slots_available, or when asked why a "
                "site is not producing new suggestions."
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
                "Search review-queue suggestions, highest score first. Filter by site, "
                "status, text, method, target origin, and a similarity band "
                "(min_percent/max_percent) — use the band to size a threshold before "
                "calling preview_bulk_review. Returns at most `limit` rows plus "
                "`next_cursor`; pass it back as `cursor` with the same filters to read "
                "the rest of a queue larger than one page."
            ),
            args_model=SearchQueueArgs,
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


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema without pydantic's ``title`` noise — models quote it back."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


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
        return {"error": f"invalid arguments: {exc.error_count()} problem(s)", "status": 422}
    try:
        result = tool.handler(db=db, principal=principal, args=args)
    except HTTPException as exc:
        return {"error": str(exc.detail), "status": exc.status_code}
    except Exception as exc:  # noqa: BLE001 - surfaces stay answerable
        return {"error": f"tool failed: {exc}", "status": 500}
    return result
