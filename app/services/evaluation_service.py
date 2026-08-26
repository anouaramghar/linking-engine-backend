from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from statistics import fmean, median
from math import floor

from sqlalchemy import exists, func, null, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.models import (
    Article,
    BulkReviewOperationItem,
    EvaluationSnapshot,
    InternalLink,
    Site,
    Suggestion,
    SuggestionEvent,
)
from app.schemas.evaluation import (
    EditorialMetrics,
    EvaluationComparison,
    EvaluationMetric,
    EvaluationMetricsOut,
    EvaluationProvenance,
    EvaluationSuggestionOut,
    EvaluationSuggestionPage,
    ExposureMetrics,
    GraphImpactMetrics,
    EvaluationTrendPoint,
    MethodMetrics,
    OrphanMetrics,
    OrphanTrendPoint,
    PlacementMetrics,
    PublicationMetrics,
    RejectionReasonMetric,
    ScoreRangeMetrics,
    SiteEvaluationMetrics,
)

ACCEPTED_STATUSES = ("approved", "applying", "applied", "failed")
DECIDED_STATUSES = (*ACCEPTED_STATUSES, "rejected")
COHORT_DEFINITION = (
    "The date range selects suggestions generated during that period; all outcome metrics "
    "describe the current result of that same suggestion cohort."
)
#: Export rows fetched per database round trip. The export streams, so this is
#: the only part of the result that is ever in memory at once.
_EXPORT_FETCH_BATCH = 1_000

#: Bumped whenever a field on ``EvaluationMetricsOut`` changes meaning, so a
#: stored or exported answer can never be compared against a differently defined
#: one without the difference being visible.
EVALUATION_SCHEMA_VERSION = "evaluation_metrics_v2"

#: Thresholds from docs/superpowers/plans/2026-08-11-evidence-driven-operations.md,
#: Workstream 5: three operator-selected representative sites, at least 100
#: individual labels each, before a default may move.
INDIVIDUAL_LABEL_TARGET = 100
BASELINE_SITE_TARGET = 3

#: Read this before reading a percentage. Every item is a reason a number here
#: cannot settle a ranking or model question, not a caveat about precision.
EVALUATION_LIMITATIONS = (
    "Operational telemetry, not an evidence artifact: there is no frozen cohort, "
    "no held-out set and no versioned comparison run.",
    "Acceptance is not correctness. It records what editors decided about what "
    "they were shown, and the queue order they were shown is itself the thing "
    "under question.",
    "Bulk decisions are counted with individual ones in every rate on this page.",
    "The cohort is whatever was generated in the selected range, so a change in "
    "crawl or analysis volume moves these rates without anything about link "
    "quality changing.",
    "Semantic score is a ranking score. It is not confidence, accuracy or quality.",
    "No site selection is frozen, so comparing two ranges compares two different mixes of sites.",
)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 4)


def _suggestion_conditions(
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list:
    conditions = []
    if site_id is not None:
        conditions.append(Suggestion.site_id == site_id)
    if date_from is not None:
        conditions.append(Suggestion.created_at >= date_from)
    if date_to is not None:
        conditions.append(Suggestion.created_at < date_to)
    return conditions


def _editorial_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> EditorialMetrics:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    status_counts = dict(
        db.execute(
            select(Suggestion.status, func.count(Suggestion.id))
            .where(*conditions)
            .group_by(Suggestion.status)
        ).all()
    )
    total = sum(status_counts.values())
    accepted = sum(status_counts.get(status, 0) for status in ACCEPTED_STATUSES)
    rejected = status_counts.get("rejected", 0)
    decisions = accepted + rejected

    duration_rows = db.execute(
        select(Suggestion.created_at, Suggestion.reviewed_at).where(
            *conditions,
            Suggestion.reviewed_at.is_not(None),
        )
    ).all()
    duration_hours = [
        (reviewed_at - created_at).total_seconds() / 3600
        for created_at, reviewed_at in duration_rows
        if reviewed_at >= created_at
    ]

    return EditorialMetrics(
        suggestions_total=total,
        pending=status_counts.get("pending", 0),
        accepted=accepted,
        rejected=rejected,
        decisions=decisions,
        acceptance_rate=_rate(accepted, decisions),
        rejection_rate=_rate(rejected, decisions),
        average_decision_hours=(round(fmean(duration_hours), 2) if duration_hours else None),
        median_decision_hours=(round(median(duration_hours), 2) if duration_hours else None),
        decision_time_sample=len(duration_hours),
    )


def _placement_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> PlacementMetrics:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    generated = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.placement_generated_at.is_not(None),
            )
        )
        or 0
    )
    successful = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.placement_generated_at.is_not(None),
                Suggestion.placement_context.is_not(None),
                Suggestion.anchor_text.is_not(None),
            )
        )
        or 0
    )
    return PlacementMetrics(
        generated=generated,
        successful=successful,
        success_rate=_rate(successful, generated),
    )


def _exposure_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> ExposureMetrics:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    suggestions = db.scalar(select(func.count(Suggestion.id)).where(*conditions)) or 0
    exposed = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.shown_at.is_not(None),
            )
        )
        or 0
    )
    exposed_decisions = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.shown_at.is_not(None),
                Suggestion.status.in_(DECIDED_STATUSES),
            )
        )
        or 0
    )
    unseen_decisions = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.shown_at.is_(None),
                Suggestion.status.in_(DECIDED_STATUSES),
            )
        )
        or 0
    )
    exposed_accepted = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.shown_at.is_not(None),
                Suggestion.status.in_(ACCEPTED_STATUSES),
            )
        )
        or 0
    )
    exposed_rejected = (
        db.scalar(
            select(func.count(Suggestion.id)).where(
                *conditions,
                Suggestion.shown_at.is_not(None),
                Suggestion.status == "rejected",
            )
        )
        or 0
    )
    return ExposureMetrics(
        suggestions=suggestions,
        exposed=exposed,
        unseen=max(0, suggestions - exposed),
        exposure_rate=_rate(exposed, suggestions),
        exposed_decisions=exposed_decisions,
        unseen_decisions=unseen_decisions,
        exposed_acceptance_rate=_rate(
            exposed_accepted,
            exposed_accepted + exposed_rejected,
        ),
    )


def _rejection_reason_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[RejectionReasonMetric]:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    counts: defaultdict[str, int] = defaultdict(int)
    rows = db.execute(
        select(SuggestionEvent.details)
        .join(Suggestion, Suggestion.id == SuggestionEvent.suggestion_id)
        .where(*conditions, SuggestionEvent.event_type == "reviewed")
    )
    for (details,) in rows:
        if not isinstance(details, dict) or details.get("to_status") != "rejected":
            continue
        counts[str(details.get("rejection_reason") or "unspecified")] += 1
    return [
        RejectionReasonMetric(reason=reason, count=count)
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _graph_impact_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> GraphImpactMetrics:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    rows = db.execute(
        select(
            Suggestion.score_components,
            Suggestion.status,
            Suggestion.shown_at,
        ).where(*conditions)
    )
    with_context = adjusted = exposed = accepted = orphan_accepted = underlinked_accepted = 0
    for components, status, shown_at in rows:
        graph = components.get("graph") if isinstance(components, dict) else None
        if not isinstance(graph, dict):
            continue
        with_context += 1
        adjusted += int(bool(graph.get("applied") or graph.get("adjustment", 0) > 0))
        exposed += int(shown_at is not None)
        accepted_status = status in ACCEPTED_STATUSES
        accepted += int(accepted_status)
        orphan_accepted += int(accepted_status and graph.get("target_orphan", False))
        underlinked_accepted += int(accepted_status and graph.get("target_underlinked", False))
    return GraphImpactMetrics(
        suggestions_with_graph_context=with_context,
        graph_adjusted_suggestions=adjusted,
        exposed_graph_suggestions=exposed,
        accepted_or_published_graph_suggestions=accepted,
        orphan_targets_accepted=orphan_accepted,
        underlinked_targets_accepted=underlinked_accepted,
    )


def _publication_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> PublicationMetrics:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    rows = dict(
        db.execute(
            select(Suggestion.status, func.count(Suggestion.id))
            .where(*conditions, Suggestion.status.in_(("applied", "failed")))
            .group_by(Suggestion.status)
        ).all()
    )
    succeeded = rows.get("applied", 0)
    failed = rows.get("failed", 0)
    completed = succeeded + failed
    return PublicationMetrics(
        completed=completed,
        succeeded=succeeded,
        failed=failed,
        success_rate=_rate(succeeded, completed),
        failure_rate=_rate(failed, completed),
    )


def _current_orphan_counts(db: Session, site_id: int | None) -> tuple[int, int]:
    article_conditions = [Article.is_active.is_(True), Site.platform != "pool"]
    if site_id is not None:
        article_conditions.append(Article.site_id == site_id)
    active_articles = (
        db.scalar(
            select(func.count(Article.id))
            .join(Site, Site.id == Article.site_id)
            .where(*article_conditions)
        )
        or 0
    )
    has_active_inbound = exists(
        select(InternalLink.id).where(
            InternalLink.target_article_id == Article.id,
            InternalLink.is_active.is_(True),
        )
    )
    remaining = (
        db.scalar(
            select(func.count(Article.id))
            .join(Site, Site.id == Article.site_id)
            .where(*article_conditions, ~has_active_inbound)
        )
        or 0
    )
    return active_articles, remaining


def _orphan_help_condition(target) -> list:
    earlier_link = aliased(InternalLink)
    had_inbound_before_application = exists(
        select(earlier_link.id).where(
            earlier_link.target_article_id == Suggestion.target_article_id,
            earlier_link.first_seen_at < Suggestion.applied_at,
        )
    ).correlate(Suggestion)
    return [
        Suggestion.status == "applied",
        Suggestion.applied_at.is_not(None),
        Suggestion.publish_outcome.in_(("inserted", "block")),
        target.site_id == Suggestion.site_id,
        ~had_inbound_before_application,
    ]


def _orphan_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> OrphanMetrics:
    active_articles, remaining = _current_orphan_counts(db, site_id)
    target = aliased(Article)
    reduction_conditions = [
        *_suggestion_conditions(site_id, date_from, date_to),
        *_orphan_help_condition(target),
    ]
    reduced = (
        db.scalar(
            select(func.count(func.distinct(Suggestion.target_article_id)))
            .join(target, target.id == Suggestion.target_article_id)
            .where(*reduction_conditions)
        )
        or 0
    )
    return OrphanMetrics(
        active_articles=active_articles,
        remaining=remaining,
        reduced_by_linkmesh=reduced,
    )


def _method_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[MethodMetrics]:
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    grouped: dict[str, dict[str, int]] = defaultdict(dict)
    for method, status, count in db.execute(
        select(Suggestion.method, Suggestion.status, func.count(Suggestion.id))
        .where(*conditions)
        .group_by(Suggestion.method, Suggestion.status)
    ):
        grouped[method][status] = count
    average_scores = dict(
        db.execute(
            select(Suggestion.method, func.avg(Suggestion.score))
            .where(*conditions)
            .group_by(Suggestion.method)
        ).all()
    )
    metrics = []
    for method, statuses in grouped.items():
        accepted = sum(statuses.get(status, 0) for status in ACCEPTED_STATUSES)
        rejected = statuses.get("rejected", 0)
        metrics.append(
            MethodMetrics(
                method=method,
                suggestions=sum(statuses.values()),
                pending=statuses.get("pending", 0),
                accepted=accepted,
                rejected=rejected,
                applied=statuses.get("applied", 0),
                acceptance_rate=_rate(accepted, accepted + rejected),
                average_semantic_score=(
                    round(float(average_scores[method]), 4)
                    if average_scores.get(method) is not None
                    else None
                ),
            )
        )
    return sorted(metrics, key=lambda item: (-item.suggestions, item.method))


def _site_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[SiteEvaluationMetrics]:
    site_query = select(Site).where(Site.platform != "pool")
    if site_id is not None:
        site_query = site_query.where(Site.id == site_id)
    sites = list(db.scalars(site_query.order_by(Site.name, Site.id)))
    if not sites:
        return []
    site_ids = [site.id for site in sites]
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    conditions.append(Suggestion.site_id.in_(site_ids))
    grouped: dict[int, dict[str, int]] = defaultdict(dict)
    for grouped_site_id, status, count in db.execute(
        select(Suggestion.site_id, Suggestion.status, func.count(Suggestion.id))
        .where(*conditions)
        .group_by(Suggestion.site_id, Suggestion.status)
    ):
        grouped[grouped_site_id][status] = count
    metrics = []
    for site in sites:
        statuses = grouped.get(site.id, {})
        accepted = sum(statuses.get(status, 0) for status in ACCEPTED_STATUSES)
        rejected = statuses.get("rejected", 0)
        metrics.append(
            SiteEvaluationMetrics(
                site_id=site.id,
                site_name=site.name,
                suggestions=sum(statuses.values()),
                pending=statuses.get("pending", 0),
                accepted=accepted,
                rejected=rejected,
                applied=statuses.get("applied", 0),
                acceptance_rate=_rate(accepted, accepted + rejected),
            )
        )
    return sorted(metrics, key=lambda item: (-item.suggestions, item.site_name, item.site_id))


def _score_range_metrics(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[ScoreRangeMetrics]:
    definitions = [(0, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
    grouped = {definition: defaultdict(int) for definition in definitions}
    rows = db.execute(
        select(Suggestion.score, Suggestion.status).where(
            *_suggestion_conditions(site_id, date_from, date_to)
        )
    )
    for score, status in rows:
        percent = max(0, min(100, floor(float(score) * 100 + 0.5)))
        bucket = next(
            definition for definition in definitions if definition[0] <= percent <= definition[1]
        )
        grouped[bucket]["suggestions"] += 1
        if status == "pending":
            grouped[bucket]["pending"] += 1
        elif status in ACCEPTED_STATUSES:
            grouped[bucket]["accepted"] += 1
        elif status == "rejected":
            grouped[bucket]["rejected"] += 1
    return [
        ScoreRangeMetrics(
            label=f"{minimum}-{maximum}%",
            minimum=minimum,
            maximum=maximum,
            suggestions=grouped[(minimum, maximum)]["suggestions"],
            pending=grouped[(minimum, maximum)]["pending"],
            accepted=grouped[(minimum, maximum)]["accepted"],
            rejected=grouped[(minimum, maximum)]["rejected"],
            acceptance_rate=_rate(
                grouped[(minimum, maximum)]["accepted"],
                grouped[(minimum, maximum)]["accepted"] + grouped[(minimum, maximum)]["rejected"],
            ),
        )
        for minimum, maximum in definitions
    ]


def _bucket_kind(date_from: datetime, date_to: datetime) -> str:
    days = max(1, (date_to - date_from).days)
    if days <= 31:
        return "day"
    if days <= 120:
        return "week"
    return "month"


def _bucket_start(value: datetime | date, kind: str) -> date:
    current = value.date() if isinstance(value, datetime) else value
    if kind == "week":
        return current - timedelta(days=current.weekday())
    if kind == "month":
        return current.replace(day=1)
    return current


def _next_bucket(value: date, kind: str) -> date:
    if kind == "week":
        return value + timedelta(days=7)
    if kind == "month":
        return (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    return value + timedelta(days=1)


def _trend(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[EvaluationTrendPoint]:
    effective_to = date_to or datetime.now(UTC)
    effective_from = date_from
    if effective_from is None:
        effective_from = db.scalar(
            select(func.min(Suggestion.created_at)).where(
                *_suggestion_conditions(site_id, None, effective_to)
            )
        )
    if effective_from is None or effective_from >= effective_to:
        return []
    kind = _bucket_kind(effective_from, effective_to)
    grouped: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = db.execute(
        select(Suggestion.created_at, Suggestion.status).where(
            *_suggestion_conditions(site_id, effective_from, effective_to)
        )
    )
    for created_at, status in rows:
        bucket = _bucket_start(created_at, kind)
        grouped[bucket]["generated"] += 1
        if status in ACCEPTED_STATUSES:
            grouped[bucket]["accepted"] += 1
        if status == "rejected":
            grouped[bucket]["rejected"] += 1
        if status == "applied":
            grouped[bucket]["applied"] += 1

    points = []
    cursor = _bucket_start(effective_from, kind)
    last = _bucket_start(effective_to - timedelta(microseconds=1), kind)
    while cursor <= last:
        values = grouped[cursor]
        accepted = values["accepted"]
        rejected = values["rejected"]
        points.append(
            EvaluationTrendPoint(
                bucket_start=cursor,
                generated=values["generated"],
                accepted=accepted,
                rejected=rejected,
                applied=values["applied"],
                acceptance_rate=_rate(accepted, accepted + rejected),
            )
        )
        cursor = _next_bucket(cursor, kind)
    return points


def _orphan_trend(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[OrphanTrendPoint]:
    conditions = []
    if site_id is not None:
        conditions.append(EvaluationSnapshot.site_id == site_id)
    if date_from is not None:
        conditions.append(EvaluationSnapshot.snapshot_date >= date_from.date())
    if date_to is not None:
        conditions.append(EvaluationSnapshot.snapshot_date <= date_to.date())
    rows = db.execute(
        select(
            EvaluationSnapshot.snapshot_date,
            func.sum(EvaluationSnapshot.active_articles),
            func.sum(EvaluationSnapshot.orphan_pages),
        )
        .where(*conditions)
        .group_by(EvaluationSnapshot.snapshot_date)
        .order_by(EvaluationSnapshot.snapshot_date)
    )
    return [
        OrphanTrendPoint(
            snapshot_date=snapshot_date,
            active_articles=active_articles,
            remaining=remaining,
        )
        for snapshot_date, active_articles, remaining in rows
    ]


def _comparison(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
    current_editorial: EditorialMetrics,
    current_placement: PlacementMetrics,
    current_publication: PublicationMetrics,
) -> EvaluationComparison | None:
    if date_from is None or date_to is None:
        return None
    span = date_to - date_from
    if span <= timedelta(0):
        return None
    previous_from = date_from - span
    previous_to = date_from
    previous_editorial = _editorial_metrics(db, site_id, previous_from, previous_to)
    previous_placement = _placement_metrics(db, site_id, previous_from, previous_to)
    previous_publication = _publication_metrics(db, site_id, previous_from, previous_to)
    suggestions_change = (
        round(
            (current_editorial.suggestions_total - previous_editorial.suggestions_total)
            / previous_editorial.suggestions_total,
            4,
        )
        if previous_editorial.suggestions_total
        else None
    )
    return EvaluationComparison(
        previous_from=previous_from,
        previous_to=previous_to,
        suggestions_change_rate=suggestions_change,
        acceptance_rate_change=_change(
            current_editorial.acceptance_rate,
            previous_editorial.acceptance_rate,
        ),
        placement_success_rate_change=_change(
            current_placement.success_rate,
            previous_placement.success_rate,
        ),
        publication_success_rate_change=_change(
            current_publication.success_rate,
            previous_publication.success_rate,
        ),
    )


def _provenance(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> EvaluationProvenance:
    """Say what these numbers are before anyone reads a percentage off them.

    The counts below separate decisions a person made row by row from decisions a
    bulk rule made for a whole filter at once. Both are real editorial outcomes
    and both belong in the operational numbers, but only the first is a *label*
    in the sense the evidence plan means, and the page must not let the two be
    read as the same evidence.
    """
    conditions = _suggestion_conditions(site_id, date_from, date_to)
    decided = [*conditions, Suggestion.status.in_(DECIDED_STATUSES)]
    from_bulk = (
        select(BulkReviewOperationItem.suggestion_id)
        .where(BulkReviewOperationItem.suggestion_id == Suggestion.id)
        .correlate(Suggestion)
        .exists()
    )
    individual = db.scalar(select(func.count()).select_from(Suggestion).where(*decided, ~from_bulk))
    bulk = db.scalar(select(func.count()).select_from(Suggestion).where(*decided, from_bulk))
    exposed_individual = db.scalar(
        select(func.count())
        .select_from(Suggestion)
        .where(*decided, Suggestion.shown_at.is_not(None), ~from_bulk)
    )
    cutoff = db.scalar(select(func.max(Suggestion.created_at)).where(*conditions))

    per_site = db.execute(
        select(Suggestion.site_id, func.count())
        .where(*decided, ~from_bulk)
        .group_by(Suggestion.site_id)
    ).all()
    qualifying = sum(1 for _, count in per_site if count >= INDIVIDUAL_LABEL_TARGET)

    if not individual and not bulk:
        state = "evidence_unavailable"
    elif qualifying >= BASELINE_SITE_TARGET:
        state = "three_site_baseline_ready"
    else:
        state = "more_individual_labels_required"

    return EvaluationProvenance(
        schema_version=EVALUATION_SCHEMA_VERSION,
        commit=settings.build_commit or None,
        evidence_cutoff=cutoff,
        individual_labels=individual or 0,
        bulk_labels=bulk or 0,
        exposed_individual_labels=exposed_individual or 0,
        label_provenance=(
            f"{individual or 0} decisions made row by row, {bulk or 0} made by a bulk rule. "
            f"{exposed_individual or 0} individual decisions were exposed before review. "
            "Both are counted in the rates on this page; only the first are individual labels."
        ),
        sample_state=state,
        sites_meeting_label_target=qualifying,
        individual_label_target=INDIVIDUAL_LABEL_TARGET,
        baseline_site_target=BASELINE_SITE_TARGET,
        limitations=list(EVALUATION_LIMITATIONS),
    )


def evaluation_metrics(
    db: Session,
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> EvaluationMetricsOut:
    editorial = _editorial_metrics(db, site_id, date_from, date_to)
    exposure = _exposure_metrics(db, site_id, date_from, date_to)
    placement = _placement_metrics(db, site_id, date_from, date_to)
    publication = _publication_metrics(db, site_id, date_from, date_to)
    return EvaluationMetricsOut(
        generated_at=datetime.now(UTC),
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
        cohort_definition=COHORT_DEFINITION,
        provenance=_provenance(db, site_id, date_from, date_to),
        editorial=editorial,
        exposure=exposure,
        rejection_reasons=_rejection_reason_metrics(db, site_id, date_from, date_to),
        graph_impact=_graph_impact_metrics(db, site_id, date_from, date_to),
        placement=placement,
        publication=publication,
        orphans=_orphan_metrics(db, site_id, date_from, date_to),
        comparison=_comparison(
            db,
            site_id,
            date_from,
            date_to,
            editorial,
            placement,
            publication,
        ),
        trend=_trend(db, site_id, date_from, date_to),
        orphan_trend=_orphan_trend(db, site_id, date_from, date_to),
        methods=_method_metrics(db, site_id, date_from, date_to),
        score_ranges=_score_range_metrics(db, site_id, date_from, date_to),
        sites=_site_metrics(db, site_id, date_from, date_to),
    )


def capture_daily_evaluation_snapshots(
    db: Session,
    snapshot_date: date | None = None,
) -> int:
    observed_on = snapshot_date or datetime.now(UTC).date()
    sites = list(db.scalars(select(Site).where(Site.platform != "pool").order_by(Site.id)))
    for site in sites:
        active_articles, orphan_pages = _current_orphan_counts(db, site.id)
        snapshot = db.scalar(
            select(EvaluationSnapshot).where(
                EvaluationSnapshot.snapshot_date == observed_on,
                EvaluationSnapshot.site_id == site.id,
            )
        )
        if snapshot is None:
            db.add(
                EvaluationSnapshot(
                    snapshot_date=observed_on,
                    site_id=site.id,
                    active_articles=active_articles,
                    orphan_pages=orphan_pages,
                )
            )
        else:
            snapshot.active_articles = active_articles
            snapshot.orphan_pages = orphan_pages
            snapshot.captured_at = datetime.now(UTC)
    db.flush()
    return len(sites)


def _drilldown_condition(metric: EvaluationMetric, target) -> list:
    if metric == "decided":
        return [Suggestion.status.in_(DECIDED_STATUSES)]
    if metric == "accepted":
        return [Suggestion.status.in_(ACCEPTED_STATUSES)]
    if metric == "rejected":
        return [Suggestion.status == "rejected"]
    if metric == "pending":
        return [Suggestion.status == "pending"]
    if metric == "placement_success":
        return [
            Suggestion.placement_generated_at.is_not(None),
            Suggestion.placement_context.is_not(None),
            Suggestion.anchor_text.is_not(None),
        ]
    if metric == "published":
        return [Suggestion.status == "applied"]
    if metric == "publish_failed":
        return [Suggestion.status == "failed"]
    return _orphan_help_condition(target)


#: When publication actually gave up on a row.
#:
#: ``suggestions`` has no failure timestamp: ``reviewed_at`` is when an editor
#: approved the row, which is the moment *before* every publication attempt, and
#: reporting it as the failure time can put the failure days before the attempt
#: that caused it. The lifecycle stream does have the moment — the trigger writes
#: ``failed`` on the transition, and the worker writes ``publish_attempt_failed``
#: per attempt. The newest of the two is the answer for both paths that end a
#: row: attempt exhaustion and plan retirement, which writes no attempt event of
#: its own. A row re-approved after failing and failed again keeps the latest,
#: which is the failure the current status describes.
_LAST_FAILURE_AT = (
    select(func.max(SuggestionEvent.created_at))
    .where(
        SuggestionEvent.suggestion_id == Suggestion.id,
        SuggestionEvent.event_type.in_(("failed", "publish_attempt_failed")),
    )
    .correlate(Suggestion)
    .scalar_subquery()
)


def _occurred_at(
    metric: EvaluationMetric,
    suggestion: Suggestion,
    last_failure_at: datetime | None = None,
) -> datetime:
    if metric == "publish_failed":
        # The fallback is for rows that failed before the lifecycle stream
        # existed; they have no event to read, and no better time than the one
        # already shown.
        return last_failure_at or suggestion.reviewed_at or suggestion.created_at
    if metric in ("decided", "accepted", "rejected"):
        return suggestion.reviewed_at or suggestion.created_at
    if metric == "placement_success":
        return suggestion.placement_generated_at or suggestion.created_at
    if metric in ("published", "orphan_helped"):
        return suggestion.applied_at or suggestion.created_at
    return suggestion.created_at


def evaluation_suggestions(
    db: Session,
    metric: EvaluationMetric,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
    offset: int,
) -> EvaluationSuggestionPage:
    source = aliased(Article)
    target = aliased(Article)
    conditions = [
        *_suggestion_conditions(site_id, date_from, date_to),
        *_drilldown_condition(metric, target),
    ]
    id_query = (
        select(Suggestion.id)
        .outerjoin(target, target.id == Suggestion.target_article_id)
        .where(*conditions)
    )
    total = db.scalar(select(func.count()).select_from(id_query.subquery())) or 0
    # Only the failure drill-down pays for the correlated lookup, and only for
    # the rows on this page.
    failure_time = _LAST_FAILURE_AT if metric == "publish_failed" else null()
    rows = db.execute(
        select(
            Suggestion,
            Site.name,
            source.title,
            func.coalesce(
                target.title,
                Suggestion.external_title,
                Suggestion.external_url,
            ),
            failure_time.label("last_failure_at"),
        )
        .join(Site, Site.id == Suggestion.site_id)
        .join(source, source.id == Suggestion.source_article_id)
        .outerjoin(target, target.id == Suggestion.target_article_id)
        .where(*conditions)
        .order_by(Suggestion.created_at.desc(), Suggestion.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        EvaluationSuggestionOut(
            id=suggestion.id,
            trace_id=suggestion.trace_id,
            site_id=suggestion.site_id,
            site_name=site_name,
            source_title=source_title,
            target_title=target_title,
            method=suggestion.method,
            score=suggestion.score,
            status=suggestion.status,
            occurred_at=_occurred_at(metric, suggestion, last_failure_at),
        )
        for suggestion, site_name, source_title, target_title, last_failure_at in rows
    ]
    return EvaluationSuggestionPage(total=total, limit=limit, offset=offset, items=items)


def evaluation_export_rows(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> Iterator[Row]:
    """Stream the export cohort, one database batch at a time.

    Deliberately an iterator and not a list: the caller writes each row straight
    into the response, and a fleet-wide export with no filters is as large as the
    suggestions table. Materializing it here would put that whole table in memory
    twice — once as rows, once as the finished file — during an ordinary request.
    """
    source = aliased(Article)
    target = aliased(Article)
    return iter(
        db.execute(
            select(
                Suggestion.id,
                Suggestion.trace_id,
                Site.name,
                source.title,
                func.coalesce(
                    target.title,
                    Suggestion.external_title,
                    Suggestion.external_url,
                ),
                Suggestion.method,
                Suggestion.score,
                Suggestion.shown_at,
                Suggestion.last_shown_at,
                Suggestion.exposure_count,
                Suggestion.status,
                Suggestion.created_at,
                Suggestion.reviewed_at,
                Suggestion.reviewer_id,
                Suggestion.rejection_reason,
                Suggestion.retrieval_version,
                Suggestion.ranking_version,
                Suggestion.final_rank,
                Suggestion.feature_snapshot,
                Suggestion.placement_generated_at,
                Suggestion.applied_at,
                Suggestion.publish_outcome,
                _LAST_FAILURE_AT.label("last_failure_at"),
            )
            .join(Site, Site.id == Suggestion.site_id)
            .join(source, source.id == Suggestion.source_article_id)
            .outerjoin(target, target.id == Suggestion.target_article_id)
            .where(*_suggestion_conditions(site_id, date_from, date_to))
            .order_by(Suggestion.created_at.desc(), Suggestion.id.desc())
            .execution_options(yield_per=_EXPORT_FETCH_BATCH)
        )
    )
