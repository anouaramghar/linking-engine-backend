from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import fmean, median
from math import floor

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from app.models import Article, EvaluationSnapshot, InternalLink, Site, Suggestion
from app.schemas.evaluation import (
    EditorialMetrics,
    EvaluationComparison,
    EvaluationMetric,
    EvaluationMetricsOut,
    EvaluationSuggestionOut,
    EvaluationSuggestionPage,
    EvaluationTrendPoint,
    MethodMetrics,
    OrphanMetrics,
    OrphanTrendPoint,
    PlacementMetrics,
    PublicationMetrics,
    ScoreRangeMetrics,
    SiteEvaluationMetrics,
)

ACCEPTED_STATUSES = ("approved", "applying", "applied", "failed")
DECIDED_STATUSES = (*ACCEPTED_STATUSES, "rejected")
COHORT_DEFINITION = (
    "The date range selects suggestions generated during that period; all outcome metrics "
    "describe the current result of that same suggestion cohort."
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
    generated = db.scalar(
        select(func.count(Suggestion.id)).where(
            *conditions,
            Suggestion.placement_generated_at.is_not(None),
        )
    ) or 0
    successful = db.scalar(
        select(func.count(Suggestion.id)).where(
            *conditions,
            Suggestion.placement_generated_at.is_not(None),
            Suggestion.placement_context.is_not(None),
            Suggestion.anchor_text.is_not(None),
        )
    ) or 0
    return PlacementMetrics(
        generated=generated,
        successful=successful,
        success_rate=_rate(successful, generated),
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
    active_articles = db.scalar(
        select(func.count(Article.id))
        .join(Site, Site.id == Article.site_id)
        .where(*article_conditions)
    ) or 0
    has_active_inbound = exists(
        select(InternalLink.id).where(
            InternalLink.target_article_id == Article.id,
            InternalLink.is_active.is_(True),
        )
    )
    remaining = db.scalar(
        select(func.count(Article.id))
        .join(Site, Site.id == Article.site_id)
        .where(*article_conditions, ~has_active_inbound)
    ) or 0
    return active_articles, remaining


def _orphan_help_condition(target) -> list:
    earlier_link = aliased(InternalLink)
    had_inbound_before_application = (
        exists(
            select(earlier_link.id).where(
                earlier_link.target_article_id == Suggestion.target_article_id,
                earlier_link.first_seen_at < Suggestion.applied_at,
            )
        )
        .correlate(Suggestion)
    )
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
    reduced = db.scalar(
        select(func.count(func.distinct(Suggestion.target_article_id)))
        .join(target, target.id == Suggestion.target_article_id)
        .where(*reduction_conditions)
    ) or 0
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
            definition
            for definition in definitions
            if definition[0] <= percent <= definition[1]
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
                grouped[(minimum, maximum)]["accepted"]
                + grouped[(minimum, maximum)]["rejected"],
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


def evaluation_metrics(
    db: Session,
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> EvaluationMetricsOut:
    editorial = _editorial_metrics(db, site_id, date_from, date_to)
    placement = _placement_metrics(db, site_id, date_from, date_to)
    publication = _publication_metrics(db, site_id, date_from, date_to)
    return EvaluationMetricsOut(
        generated_at=datetime.now(UTC),
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
        cohort_definition=COHORT_DEFINITION,
        editorial=editorial,
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


def _occurred_at(metric: EvaluationMetric, suggestion: Suggestion) -> datetime:
    if metric in ("decided", "accepted", "rejected", "publish_failed"):
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
            occurred_at=_occurred_at(metric, suggestion),
        )
        for suggestion, site_name, source_title, target_title in rows
    ]
    return EvaluationSuggestionPage(total=total, limit=limit, offset=offset, items=items)


def evaluation_export_rows(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[tuple]:
    source = aliased(Article)
    target = aliased(Article)
    return list(
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
                Suggestion.status,
                Suggestion.created_at,
                Suggestion.reviewed_at,
                Suggestion.placement_generated_at,
                Suggestion.applied_at,
                Suggestion.publish_outcome,
            )
            .join(Site, Site.id == Suggestion.site_id)
            .join(source, source.id == Suggestion.source_article_id)
            .outerjoin(target, target.id == Suggestion.target_article_id)
            .where(*_suggestion_conditions(site_id, date_from, date_to))
            .order_by(Suggestion.created_at.desc(), Suggestion.id.desc())
        )
    )
