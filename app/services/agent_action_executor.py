"""Closed dispatcher for proposals produced by LinkMesh preview tools.

No endpoint string is fetched dynamically. Each supported proposal maps to the
same route function used by the dashboard, preserving its Pydantic validation,
authorization checks, optimistic guards, audit fields, and task enqueue logic.
"""

import re
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.api.routes import alerts, ingestion, pipelines, sites, suggestions
from app.schemas.alert import AlertAcknowledgeGuard
from app.schemas.external_policy import ExternalLinkPolicyUpdate
from app.schemas.job import ArticleAnalysisStartGuard, JobStartGuard
from app.schemas.pipeline import PipelineBatchCreate, PipelineCancelGuard, PipelineRetryGuard
from app.schemas.site import (
    EditorialRankingPolicyUpdate,
    PoolSourceActionGuard,
    SiteBulkRequest,
    SiteCreateRequest,
)
from app.schemas.suggestion import BulkReviewFilter, SuggestionReview
from app.services.authorization import Principal, authorize_site


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if hasattr(value, "__table__"):
        return jsonable_encoder(
            {column.name: getattr(value, column.name) for column in value.__table__.columns}
        )
    return jsonable_encoder(value)


def _match(proposal: dict, *, tool: str, kind: str, method: str, path: str) -> re.Match | None:
    if (
        proposal.get("tool") != tool
        or proposal.get("kind") != kind
        or proposal.get("method") != method
    ):
        return None
    endpoint = str(proposal.get("endpoint", ""))
    return re.fullmatch(path, endpoint)


def execute_proposal(
    db: Session,
    principal: Principal,
    proposal: dict,
    *,
    actor: str,
) -> dict:
    payload = proposal.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(400, "the confirmed action payload is invalid")
    try:
        if _match(
            proposal,
            tool="preview_bulk_review",
            kind="bulk_review",
            method="POST",
            path=r"/api/v1/suggestions/bulk-review-by-filter",
        ):
            result = suggestions.bulk_review_by_filter(
                BulkReviewFilter.model_validate(payload), actor, principal, db
            )
            return {"message": f"Applied {result.reviewed} reviews.", "result": _json(result)}

        if _match(
            proposal,
            tool="preview_site_creation",
            kind="site_create",
            method="POST",
            path=r"/api/v1/sites",
        ):
            result = sites.create_site(
                SiteCreateRequest.model_validate(payload), None, principal, db
            )
            return {
                "message": f"Connected {result.name} as site #{result.id}.",
                "result": _json(result),
            }

        if _match(
            proposal,
            tool="preview_site_creation",
            kind="site_bulk_create",
            method="POST",
            path=r"/api/v1/sites/bulk",
        ):
            result = sites.bulk_create_sites(
                SiteBulkRequest.model_validate(payload), None, principal, db
            )
            return {"message": f"Connected {len(result.created)} sites.", "result": _json(result)}

        matched = _match(
            proposal,
            tool="preview_alert_acknowledgement",
            kind="alert_acknowledgement",
            method="POST",
            path=r"/api/v1/alerts/(\d+)/acknowledge",
        )
        if matched:
            result = alerts.acknowledge_alert(
                int(matched[1]), AlertAcknowledgeGuard.model_validate(payload), principal, db
            )
            return {"message": f"Acknowledged alert #{result.id}.", "result": _json(result)}

        matched = _match(
            proposal,
            tool="preview_pool_source_action",
            kind="pool_source_action",
            method=str(proposal.get("method")),
            path=r"/api/v1/sites/(\d+)/pool-source/(approval|reactivate)",
        )
        if matched and proposal.get("method") in {"POST", "DELETE"}:
            site = authorize_site(db, principal, int(matched[1]))
            guard = PoolSourceActionGuard.model_validate(payload)
            action = (proposal.get("context") or {}).get("action")
            if action == "approve" and matched[2] == "approval" and proposal["method"] == "POST":
                result = sites.approve_pool_source(guard, site, db, actor)
            elif action == "revoke" and matched[2] == "approval" and proposal["method"] == "DELETE":
                result = sites.revoke_pool_source_approval(guard, site, db, actor)
            elif (
                action == "reactivate"
                and matched[2] == "reactivate"
                and proposal["method"] == "POST"
            ):
                result = sites.reactivate_pool_source(guard, site, db, actor)
            else:
                raise HTTPException(400, "unsupported confirmed pool-source action")
            return {
                "message": f"{action.title()}d pool source #{site.id}.",
                "result": _json(result),
            }

        matched = _match(
            proposal,
            tool="preview_site_job",
            kind="site_job_start",
            method="POST",
            path=r"/api/v1/(sites|suggestions)/(\d+)(/ingest)?",
        )
        if matched:
            site = authorize_site(db, principal, int(matched[2]))
            guard = JobStartGuard.model_validate(payload)
            if matched[1] == "sites" and matched[3] == "/ingest":
                result = ingestion.trigger_ingestion(guard, site, db)
                label = "crawl"
            elif matched[1] == "suggestions" and matched[3] is None:
                result = suggestions.trigger_analysis(guard, site, db)
                label = "analysis"
            else:
                raise HTTPException(400, "unsupported confirmed site job")
            return {
                "message": f"Started {label} job #{result.job_run_id}.",
                "result": _json(result),
            }

        matched = _match(
            proposal,
            tool="preview_article_analysis",
            kind="article_analysis_start",
            method="POST",
            path=r"/api/v1/articles/(\d+)/suggestions",
        )
        if matched:
            result = suggestions.trigger_article_analysis(
                int(matched[1]), ArticleAnalysisStartGuard.model_validate(payload), principal, db
            )
            return {
                "message": f"Started article analysis job #{result.job_run_id}.",
                "result": _json(result),
            }

        if _match(
            proposal,
            tool="preview_pipeline_batch",
            kind="pipeline_batch_start",
            method="POST",
            path=r"/api/v1/pipelines/batches",
        ):
            result = pipelines.create_pipeline_batch(
                PipelineBatchCreate.model_validate(payload), principal, db
            )
            return {"message": f"Started pipeline batch #{result.id}.", "result": _json(result)}

        matched = _match(
            proposal,
            tool="preview_pipeline_retry",
            kind="pipeline_retry",
            method="POST",
            path=r"/api/v1/pipelines/batches/(\d+)/sites/(\d+)/retry",
        )
        if matched:
            result = pipelines.retry_pipeline_site(
                int(matched[1]),
                int(matched[2]),
                PipelineRetryGuard.model_validate(payload),
                principal,
                db,
            )
            return {
                "message": f"Retried site #{matched[2]} in batch #{matched[1]}.",
                "result": _json(result),
            }

        matched = _match(
            proposal,
            tool="preview_pipeline_cancel",
            kind="pipeline_cancel",
            method="POST",
            path=r"/api/v1/pipelines/batches/(\d+)/cancel",
        )
        if matched:
            result = pipelines.cancel_pipeline_batch(
                int(matched[1]), PipelineCancelGuard.model_validate(payload), principal, db, actor
            )
            return {"message": f"Cancelled pipeline batch #{matched[1]}.", "result": _json(result)}

        matched = _match(
            proposal,
            tool="preview_suggestion_review",
            kind="review_suggestion",
            method="PUT",
            path=r"/api/v1/suggestions/(\d+)",
        )
        if matched:
            result = suggestions.review_suggestion(
                int(matched[1]), SuggestionReview.model_validate(payload), actor, principal, db
            )
            return {
                "message": f"Suggestion #{result.id} is {result.status}.",
                "result": _json(result),
            }

        matched = _match(
            proposal,
            tool="preview_editorial_ranking_policy",
            kind="editorial_ranking_policy",
            method="PUT",
            path=r"/api/v1/sites/(\d+)/editorial-ranking-policy",
        )
        if matched:
            site = authorize_site(db, principal, int(matched[1]))
            result = sites.update_editorial_ranking_policy(
                EditorialRankingPolicyUpdate.model_validate(payload), site, db
            )
            return {
                "message": f"Updated ranking policy for site #{site.id}.",
                "result": _json(result),
            }

        matched = _match(
            proposal,
            tool="preview_external_link_policy",
            kind="external_link_policy",
            method="PUT",
            path=r"/api/v1/sites/(\d+)/external-link-policy",
        )
        if matched:
            site = authorize_site(db, principal, int(matched[1]))
            result = sites.update_external_link_policy(
                ExternalLinkPolicyUpdate.model_validate(payload), site, db, actor
            )
            return {
                "message": f"Updated external-link policy for site #{site.id}.",
                "result": _json(result),
            }
    except ValidationError as error:
        raise HTTPException(400, "the confirmed action payload no longer validates") from error

    raise HTTPException(400, "this confirmed action is not in the MCP execution allowlist")
