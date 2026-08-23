from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    get_audit_actor,
    get_db,
    require_api_key,
    require_operator_identity,
    require_site_access,
    require_site_read,
)
from app.api.pagination import MAX_PAGE_SIZE
from app.config import settings
from app.models import (
    Article,
    ExternalLinkPolicy,
    IngestionRun,
    InternalLink,
    JobRun,
    PoolSourceAuditEvent,
    Site,
    Suggestion,
)
from app.schemas.external_policy import (
    ExternalLinkPolicyOut,
    ExternalLinkPolicyUpdate,
    ExternalSourceEvaluationList,
)
from app.services.authorization import (
    Principal,
    authorize_site_read,
    readable_site_filter,
    require_creatable_platform,
    resolve_create_tenant_id,
)
from app.schemas.pool_audit import PoolSourceAuditEventOut
from app.schemas.site import (
    ArticleOut,
    EditorialRankingPolicyOut,
    EditorialRankingPolicyUpdate,
    PoolSourceValidationRequest,
    PoolSourceValidationResult,
    SiteBulkCreated,
    SiteBulkFailure,
    SiteBulkRequest,
    SiteBulkResult,
    SiteCreate,
    SiteCredentials,
    SiteOut,
)
from app.services.external_link_policy import (
    PolicyState,
    expire_ineligible_external_suggestions,
    ineligible_external_suggestions,
    policy_state,
    source_evaluations,
)
from app.services.ingestion_service import latest_run
from app.services.pool_source_audit import record_pool_source_audit_event
from app.services.pool_source_policy import (
    PoolSourceFetchError,
    PoolSourcePolicyError,
    expire_pool_target_suggestions,
    require_allowed_pool_domain,
    require_no_pbn_conflict,
)
from app.services.pool_source_validation import classify_pool_source, probe_pool_source

router = APIRouter(prefix="/sites", tags=["sites"])

DUPLICATE_REASON = "a site with this base_url already exists"

#: How much of a failure message travels on a list row. Long enough for the
#: sentence that names the cause, short enough that 250 failed sites do not
#: turn one page into a megabyte of tracebacks.
MAX_ROW_ERROR_CHARS = 300


def _row_error(message: str | None) -> str | None:
    if not message:
        return None
    trimmed = " ".join(message.split())
    if len(trimmed) <= MAX_ROW_ERROR_CHARS:
        return trimmed
    return trimmed[: MAX_ROW_ERROR_CHARS - 1].rstrip() + "…"


def _managed_site_or_409(site: Site) -> Site:
    if site.platform == "pool":
        raise HTTPException(409, "external-link policies belong to managed sites")
    return site


def _external_policy_out(
    db: Session, site_id: int, *, expired_suggestions: int = 0
) -> ExternalLinkPolicyOut:
    state = policy_state(db, site_id)
    stored = db.get(ExternalLinkPolicy, site_id)
    return ExternalLinkPolicyOut(
        **state.as_payload(),
        updated_by=stored.updated_by if stored is not None else None,
        updated_at=stored.updated_at if stored is not None else None,
        expired_suggestions=expired_suggestions,
    )


def _first_error(exc: ValidationError) -> str:
    """Flatten a row's validation failure into one reviewer-readable line."""
    error = exc.errors()[0]
    message = error["msg"].removeprefix("Value error, ")
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {message}" if location else message


def _site_counts(
    db: Session,
    site_ids: list[int],
) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
    if not site_ids:
        return {}, {}, {}

    article_counts = dict(
        db.execute(
            select(Article.site_id, func.count(Article.id))
            .where(
                Article.site_id.in_(site_ids),
                Article.is_active.is_(True),
            )
            .group_by(Article.site_id)
        ).all()
    )
    internal_link_counts = dict(
        db.execute(
            select(Article.site_id, func.count(InternalLink.id))
            .join(InternalLink, InternalLink.source_article_id == Article.id)
            .where(
                Article.site_id.in_(site_ids),
                Article.is_active.is_(True),
                InternalLink.is_active.is_(True),
            )
            .group_by(Article.site_id)
        ).all()
    )
    active_suggestion_counts = dict(
        db.execute(
            select(Suggestion.site_id, func.count(Suggestion.id))
            .join(Article, Article.id == Suggestion.source_article_id)
            .where(
                Suggestion.site_id.in_(site_ids),
                Article.is_active.is_(True),
                Suggestion.status.in_(("pending", "approved", "applying")),
            )
            .group_by(Suggestion.site_id)
        ).all()
    )
    return article_counts, internal_link_counts, active_suggestion_counts


def _latest_analyses(db: Session, site_ids: list[int]) -> dict[int, JobRun]:
    """The last *finished* analysis run per site.

    A crawl and an analysis are separate jobs, so the crawl run alone cannot say
    whether a site has suggestions yet. In-flight analyses already reach the UI
    through the active-jobs feed, so a resting row only needs the last outcome.
    """
    if not site_ids:
        return {}

    newest = (
        select(func.max(JobRun.id))
        .where(
            JobRun.site_id.in_(site_ids),
            JobRun.kind == "analysis",
            JobRun.status.in_(("succeeded", "failed")),
        )
        .group_by(JobRun.site_id)
        .scalar_subquery()
    )
    runs = db.scalars(select(JobRun).where(JobRun.id.in_(newest))).all()
    return {run.site_id: run for run in runs}


def _latest_ingestions(db: Session, site_ids: list[int]) -> dict[int, IngestionRun]:
    """The newest crawl per listed site in one query, not one query per row."""
    if not site_ids:
        return {}
    ranked = (
        select(
            IngestionRun.id.label("run_id"),
            func.row_number()
            .over(
                partition_by=IngestionRun.site_id,
                order_by=(IngestionRun.started_at.desc(), IngestionRun.id.desc()),
            )
            .label("position"),
        )
        .where(IngestionRun.site_id.in_(site_ids))
        .subquery()
    )
    runs = db.scalars(
        select(IngestionRun)
        .join(ranked, ranked.c.run_id == IngestionRun.id)
        .where(ranked.c.position == 1)
    ).all()
    return {run.site_id: run for run in runs}


def _site_out(
    site: Site,
    *,
    article_count: int,
    internal_link_count: int,
    active_suggestion_count: int,
    run: IngestionRun | None,
    analysis: JobRun | None = None,
) -> SiteOut:
    item = SiteOut.model_validate(site)
    site_capacity = min(
        article_count * settings.hybrid_max_suggestions_per_article,
        settings.hybrid_max_active_suggestions_per_site,
    )
    item.suggestion_slots_available = max(0, site_capacity - active_suggestion_count)
    if site.platform == "pool":
        item.suggestion_slots_available = 0
    item.article_count = article_count
    item.internal_link_count = internal_link_count
    if run is not None:
        item.last_ingestion_status = run.status
        item.last_crawl_at = run.finished_at or run.started_at
        # Only for a failure: a message left on a succeeded run would read as a
        # problem the site does not have.
        if run.status == "failed":
            item.last_ingestion_error = _row_error(run.error)
    if analysis is not None:
        item.last_analysis_status = analysis.status
        item.last_analysis_at = analysis.finished_at or analysis.enqueued_at
        if analysis.status == "failed":
            item.last_analysis_error = _row_error(analysis.error)
    return item


@router.post("", status_code=201, response_model=SiteOut)
def create_site(
    payload: SiteCreate,
    tenant_id: int | None = Query(None, ge=1),
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> Site:
    owner_tenant_id = resolve_create_tenant_id(db, principal, tenant_id=tenant_id)
    require_creatable_platform(principal, payload.platform)
    # Scoped to the owner: a URL another tenant already holds is not this
    # tenant's conflict, and reporting it would expose their inventory.
    if db.scalar(
        select(Site.id).where(
            Site.base_url == payload.base_url,
            Site.tenant_id == owner_tenant_id,
        )
    ):
        raise HTTPException(409, DUPLICATE_REASON)
    if payload.platform != "pool":
        try:
            require_no_pbn_conflict(db, payload.base_url, as_pool=False)
        except PoolSourcePolicyError as error:
            raise HTTPException(409, str(error)) from error
    site = Site(**payload.model_dump(), tenant_id=owner_tenant_id)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


@router.post("/bulk", response_model=SiteBulkResult)
def bulk_create_sites(
    payload: SiteBulkRequest,
    tenant_id: int | None = Query(None, ge=1),
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> SiteBulkResult:
    """Create many sites in one request, reporting the outcome of every row.

    Partial success is the contract: a row that fails validation or collides with an
    existing site is reported and skipped, and the rest of the upload still lands. Each
    insert runs in its own savepoint so one collision cannot poison the batch.
    """
    created: list[SiteBulkCreated] = []
    skipped: list[SiteBulkFailure] = []
    rejected: list[SiteBulkFailure] = []
    seen: set[str] = set()
    owner_tenant_id = resolve_create_tenant_id(db, principal, tenant_id=tenant_id)

    for index, row in enumerate(payload.sites, start=1):
        try:
            item = SiteCreate.model_validate(row.model_dump())
        except ValidationError as exc:
            rejected.append(
                SiteBulkFailure(row=index, base_url=row.base_url, reason=_first_error(exc))
            )
            continue

        # Checked against the validated row, not the raw one, so a differently
        # cased "POOL" cannot slip past. Authorization aborts the whole upload
        # rather than reporting a skipped row: nothing is committed until the
        # end of the loop, so the batch lands all-or-nothing on this path.
        require_creatable_platform(principal, item.platform)

        # `item.base_url` is normalized by SiteCreate, so both checks compare like for like.
        if item.base_url in seen:
            skipped.append(
                SiteBulkFailure(
                    row=index,
                    base_url=item.base_url,
                    reason="duplicate base_url within this upload",
                )
            )
            continue
        seen.add(item.base_url)

        if db.scalar(
            select(Site.id).where(
                Site.base_url == item.base_url,
                Site.tenant_id == owner_tenant_id,
            )
        ):
            skipped.append(
                SiteBulkFailure(row=index, base_url=item.base_url, reason=DUPLICATE_REASON)
            )
            continue

        if item.platform != "pool":
            try:
                require_no_pbn_conflict(db, item.base_url, as_pool=False)
            except PoolSourcePolicyError as error:
                rejected.append(
                    SiteBulkFailure(row=index, base_url=item.base_url, reason=str(error))
                )
                continue

        site = Site(**item.model_dump(), tenant_id=owner_tenant_id)
        try:
            with db.begin_nested():
                db.add(site)
                db.flush()
        except IntegrityError:  # a concurrent import claimed the same base_url
            skipped.append(
                SiteBulkFailure(row=index, base_url=item.base_url, reason=DUPLICATE_REASON)
            )
            continue

        created.append(
            SiteBulkCreated(row=index, id=site.id, name=site.name, base_url=site.base_url)
        )

    db.commit()
    return SiteBulkResult(created=created, skipped=skipped, rejected=rejected)


@router.post("/pool-source/validate", response_model=PoolSourceValidationResult)
def validate_pool_source(
    payload: PoolSourceValidationRequest,
    tenant_id: int | None = Query(None, ge=1),
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PoolSourceValidationResult:
    """Validate one remote pool source without creating, approving, or crawling it."""

    require_creatable_platform(principal, "pool")
    owner_tenant_id = resolve_create_tenant_id(db, principal, tenant_id=tenant_id)
    try:
        item = SiteCreate.model_validate(
            {"name": payload.name, "base_url": payload.base_url, "platform": "pool"}
        )
    except ValidationError as exc:
        return PoolSourceValidationResult(
            base_url=payload.base_url,
            valid=False,
            reason=_first_error(exc),
        )

    source_type = classify_pool_source(item.base_url)
    if db.scalar(
        select(Site.id).where(
            Site.base_url == item.base_url,
            Site.tenant_id == owner_tenant_id,
        )
    ):
        return PoolSourceValidationResult(
            base_url=item.base_url,
            valid=False,
            source_type=source_type,
            reason=DUPLICATE_REASON,
        )

    try:
        require_allowed_pool_domain(item.base_url)
        require_no_pbn_conflict(db, item.base_url, as_pool=True)
        source_type = probe_pool_source(Site(**item.model_dump(), tenant_id=owner_tenant_id))
    except (PoolSourcePolicyError, PoolSourceFetchError) as error:
        return PoolSourceValidationResult(
            base_url=item.base_url,
            valid=False,
            source_type=source_type,
            reason=str(error),
        )

    return PoolSourceValidationResult(
        base_url=item.base_url,
        valid=True,
        source_type=source_type,
    )


@router.get("", response_model=list[SiteOut])
def list_sites(
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, min_length=1, max_length=255),
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> list[SiteOut]:
    query = select(Site)
    readable = readable_site_filter(principal)
    if readable is not None:
        query = query.where(readable)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            Site.name.ilike(pattern) | Site.base_url.ilike(pattern) | Site.platform.ilike(pattern)
        )
    sites = db.scalars(query.order_by(Site.id).limit(limit).offset(offset)).all()
    article_counts, internal_link_counts, active_suggestion_counts = _site_counts(
        db, [site.id for site in sites]
    )
    analyses = _latest_analyses(db, [site.id for site in sites])
    ingestions = _latest_ingestions(db, [site.id for site in sites])
    out = []
    for site in sites:
        out.append(
            _site_out(
                site,
                article_count=article_counts.get(site.id, 0),
                internal_link_count=internal_link_counts.get(site.id, 0),
                active_suggestion_count=active_suggestion_counts.get(site.id, 0),
                run=ingestions.get(site.id),
                analysis=analyses.get(site.id),
            )
        )
    return out


@router.get("/{site_id}/external-link-policy", response_model=ExternalLinkPolicyOut)
def get_external_link_policy(
    site: Site = Depends(require_site_read), db: Session = Depends(get_db)
) -> ExternalLinkPolicyOut:
    _managed_site_or_409(site)
    return _external_policy_out(db, site.id)


@router.put("/{site_id}/external-link-policy", response_model=ExternalLinkPolicyOut)
def update_external_link_policy(
    payload: ExternalLinkPolicyUpdate,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
    operator_id: str = Depends(get_audit_actor),
) -> ExternalLinkPolicyOut:
    _managed_site_or_409(site)
    current_values = policy_state(db, site.id).as_payload()
    current_values.pop("site_id")
    if payload.expected is not None and current_values != payload.expected.model_dump():
        raise HTTPException(
            409,
            "external link policy changed since it was previewed; refresh before saving",
        )
    special = {"expected", "expected_expiring_suggestion_ids"}
    updates = payload.model_dump(exclude_unset=True, exclude=special)
    desired = {**current_values, **updates}
    proposed_policy = PolicyState(
        site_id=site.id,
        external_links_enabled=desired["external_links_enabled"],
        require_https=desired["require_https"],
        min_trust_score=desired["min_trust_score"],
        min_domain_age_days=desired["min_domain_age_days"],
        trusted_tlds=tuple(desired["trusted_tlds"]),
        allowlist_domains=tuple(desired["allowlist_domains"]),
        blocklist_domains=tuple(desired["blocklist_domains"]),
        competitor_domains=tuple(desired["competitor_domains"]),
    )
    if payload.expected_expiring_suggestion_ids is not None:
        actual_expiring_ids = sorted(
            suggestion.id
            for suggestion, _evaluation, _details_key in ineligible_external_suggestions(
                db, site, policy=proposed_policy
            )
        )
        if actual_expiring_ids != payload.expected_expiring_suggestion_ids:
            raise HTTPException(
                409,
                "external link policy impact changed since it was previewed; refresh before saving",
            )
    policy = db.get(ExternalLinkPolicy, site.id)
    if policy is None:
        policy = ExternalLinkPolicy(site_id=site.id)
        db.add(policy)
    # The policy surface accepts partial updates in practice.  Do not turn an
    # omitted field into its schema default: an existing explicitly enabled
    # policy must remain enabled when an operator changes only a domain rule.
    for field, value in updates.items():
        setattr(policy, field, value)
    policy.updated_by = operator_id
    db.flush()
    expired = expire_ineligible_external_suggestions(db, site, actor=operator_id)
    db.commit()
    db.refresh(policy)
    return _external_policy_out(db, site.id, expired_suggestions=expired)


@router.get(
    "/{site_id}/external-link-policy/sources",
    response_model=ExternalSourceEvaluationList,
)
def list_external_source_evaluations(
    site: Site = Depends(require_site_read), db: Session = Depends(get_db)
) -> ExternalSourceEvaluationList:
    _managed_site_or_409(site)
    return ExternalSourceEvaluationList(items=source_evaluations(db, site))


def _editorial_policy_out(site: Site) -> EditorialRankingPolicyOut:
    return EditorialRankingPolicyOut(
        site_id=site.id,
        enabled=site.editorial_feedback_enabled,
        min_score_percent=site.editorial_min_score_percent,
        feedback_weight=site.editorial_feedback_weight,
        min_samples=site.editorial_feedback_min_samples,
    )


@router.get("/{site_id}/editorial-ranking-policy", response_model=EditorialRankingPolicyOut)
def get_editorial_ranking_policy(
    site: Site = Depends(require_site_read),
) -> EditorialRankingPolicyOut:
    return _editorial_policy_out(_managed_site_or_409(site))


@router.put("/{site_id}/editorial-ranking-policy", response_model=EditorialRankingPolicyOut)
def update_editorial_ranking_policy(
    payload: EditorialRankingPolicyUpdate,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> EditorialRankingPolicyOut:
    _managed_site_or_409(site)
    current = _editorial_policy_out(site)
    if payload.expected is not None:
        current_values = current.model_dump(exclude={"site_id"})
        if current_values != payload.expected.model_dump():
            raise HTTPException(
                409,
                "editorial ranking policy changed since it was previewed; refresh before saving",
            )
    site.editorial_feedback_enabled = payload.enabled
    site.editorial_min_score_percent = payload.min_score_percent
    site.editorial_feedback_weight = payload.feedback_weight
    site.editorial_feedback_min_samples = payload.min_samples
    db.commit()
    db.refresh(site)
    return _editorial_policy_out(site)


def _fresh_site_out(db: Session, site: Site) -> SiteOut:
    article_counts, internal_link_counts, active_suggestion_counts = _site_counts(db, [site.id])
    run = latest_run(db, site.id)
    return _site_out(
        site,
        article_count=article_counts.get(site.id, 0),
        internal_link_count=internal_link_counts.get(site.id, 0),
        active_suggestion_count=active_suggestion_counts.get(site.id, 0),
        run=run,
        analysis=_latest_analyses(db, [site.id]).get(site.id),
    )


@router.get(
    "/{site_id}/pool-source/audit-events",
    response_model=list[PoolSourceAuditEventOut],
)
def list_pool_source_audit_events(
    site_id: int,
    principal: Principal = Depends(require_api_key),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[PoolSourceAuditEvent]:
    # The audit trail intentionally survives site deletion (traceability), so
    # keep reading it for a missing site — but only for a principal broad
    # enough that its ownership cannot be checked against a deleted site.
    site = db.get(Site, site_id)
    if site is not None:
        authorize_site_read(db, principal, site.id)
    elif not principal.is_admin:
        raise HTTPException(status_code=403, detail="access denied for this site")
    return db.scalars(
        select(PoolSourceAuditEvent)
        .where(PoolSourceAuditEvent.site_id == site_id)
        .order_by(PoolSourceAuditEvent.created_at.desc(), PoolSourceAuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()


@router.get("/{site_id}", response_model=SiteOut)
def get_site(
    site: Site = Depends(require_site_read),
    db: Session = Depends(get_db),
) -> SiteOut:
    return _fresh_site_out(db, site)


@router.post("/{site_id}/pool-source/approval", response_model=SiteOut)
def approve_pool_source(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
    operator_id: str = Depends(require_operator_identity),
) -> SiteOut:
    if site.platform != "pool":
        raise HTTPException(409, f"site {site.id} is not a content-pool source")
    try:
        require_allowed_pool_domain(site.base_url)
        require_no_pbn_conflict(db, site.base_url, as_pool=True)
    except PoolSourcePolicyError as error:
        raise HTTPException(409, str(error)) from error
    site.pool_source_approved = True
    site.pool_source_approved_at = datetime.now(UTC)
    site.pool_source_approved_by = operator_id
    record_pool_source_audit_event(db, site, "approved", operator_id)
    db.commit()
    db.refresh(site)
    return _fresh_site_out(db, site)


@router.delete("/{site_id}/pool-source/approval", response_model=SiteOut)
def revoke_pool_source_approval(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
    operator_id: str = Depends(require_operator_identity),
) -> SiteOut:
    if site.platform != "pool":
        raise HTTPException(409, f"site {site.id} is not a content-pool source")
    site.pool_source_approved = False
    site.pool_source_approved_at = None
    site.pool_source_approved_by = None
    expire_pool_target_suggestions(db, site.id, reason="revoked")
    record_pool_source_audit_event(db, site, "revoked", operator_id)
    db.commit()
    db.refresh(site)
    return _fresh_site_out(db, site)


@router.post("/{site_id}/pool-source/reactivate", response_model=SiteOut)
def reactivate_pool_source(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
    operator_id: str = Depends(require_operator_identity),
) -> SiteOut:
    if site.platform != "pool":
        raise HTTPException(409, f"site {site.id} is not a content-pool source")
    if not site.pool_source_approved:
        raise HTTPException(409, f"pool source site {site.id} must be approved first")
    try:
        require_allowed_pool_domain(site.base_url)
    except PoolSourcePolicyError as error:
        raise HTTPException(409, str(error)) from error
    site.pool_source_consecutive_failures = 0
    site.pool_source_quarantined = False
    site.pool_source_quarantined_at = None
    site.pool_source_quarantine_reason = None
    site.pool_source_last_reactivated_at = datetime.now(UTC)
    site.pool_source_last_reactivated_by = operator_id
    record_pool_source_audit_event(db, site, "reactivated", operator_id)
    db.commit()
    db.refresh(site)
    return _fresh_site_out(db, site)


@router.put("/{site_id}/credentials", response_model=SiteOut)
def set_wordpress_credentials(
    payload: SiteCredentials,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> SiteOut:
    """Give an existing site a WordPress account, or replace the one it has.

    Creation is the only other place a credential can be set, so before this an
    application password that was revoked, rotated, or simply never supplied
    left the site permanently unable to publish: the only route back was
    deleting the site and losing its articles, links, and review history.

    Replacing is deliberately the same call as setting. WordPress hashes an
    application password, so the old value cannot be read back and compared, and
    a "change" that had to prove the previous value would be unusable exactly
    when it is needed — after the old one stopped working.
    """
    if site.platform != "wordpress":
        raise HTTPException(409, "WordPress credentials are only valid for WordPress sites")
    site.wp_username = payload.wp_username
    site.wp_app_password = payload.wp_app_password
    db.commit()
    db.refresh(site)
    return _fresh_site_out(db, site)


@router.delete("/{site_id}/credentials", response_model=SiteOut)
def clear_wordpress_credentials(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> SiteOut:
    """Detach the account without deleting the site.

    The site keeps crawling public pages; it stops being publishable, and the
    queue says so before anyone prepares edits for it.
    """
    site.wp_username = None
    site.wp_app_password = None
    db.commit()
    db.refresh(site)
    return _fresh_site_out(db, site)


@router.delete("/{site_id}", status_code=204)
def delete_site(
    confirm_name: str = Query(
        ...,
        min_length=1,
        description="Must exactly match the site name; stops accidental and CSRF-driven deletes.",
    ),
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> None:
    if confirm_name != site.name:
        raise HTTPException(
            409,
            "confirm_name must exactly match the site name",
        )
    db.delete(site)  # ON DELETE CASCADE takes everything else
    db.commit()


@router.get("/{site_id}/articles", response_model=list[ArticleOut])
def list_articles(
    site: Site = Depends(require_site_read),
    orphans: bool = False,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Article]:
    query = select(Article).where(
        Article.site_id == site.id,
        Article.is_active.is_(True),
    )
    if orphans:  # Expired links do not count (Phase 0, finding 3).
        query = query.where(
            ~exists().where(
                InternalLink.target_article_id == Article.id,
                InternalLink.is_active.is_(True),
            )
        )
    return db.scalars(query.order_by(Article.id).limit(limit).offset(offset)).all()
