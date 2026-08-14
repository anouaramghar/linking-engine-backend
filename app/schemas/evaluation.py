from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

EvaluationMetric = Literal[
    "decided",
    "accepted",
    "rejected",
    "pending",
    "placement_success",
    "published",
    "publish_failed",
    "orphan_helped",
]


class EditorialMetrics(BaseModel):
    suggestions_total: int
    pending: int
    accepted: int
    rejected: int
    decisions: int
    acceptance_rate: float | None
    rejection_rate: float | None
    average_decision_hours: float | None
    median_decision_hours: float | None
    decision_time_sample: int


class ExposureMetrics(BaseModel):
    """Whether a suggestion was actually rendered before its decision."""

    suggestions: int
    exposed: int
    unseen: int
    exposure_rate: float | None
    exposed_decisions: int
    unseen_decisions: int
    exposed_acceptance_rate: float | None


class RejectionReasonMetric(BaseModel):
    reason: str
    count: int


class GraphImpactMetrics(BaseModel):
    """Observed graph context and outcomes attached to generated suggestions."""

    suggestions_with_graph_context: int
    graph_adjusted_suggestions: int
    exposed_graph_suggestions: int
    accepted_or_published_graph_suggestions: int
    orphan_targets_accepted: int
    underlinked_targets_accepted: int


class PlacementMetrics(BaseModel):
    generated: int
    successful: int
    success_rate: float | None


class PublicationMetrics(BaseModel):
    completed: int
    succeeded: int
    failed: int
    success_rate: float | None
    failure_rate: float | None


class OrphanMetrics(BaseModel):
    active_articles: int
    remaining: int
    reduced_by_linkmesh: int


class MethodMetrics(BaseModel):
    method: str
    suggestions: int
    pending: int
    accepted: int
    rejected: int
    applied: int
    acceptance_rate: float | None
    average_semantic_score: float | None


class ScoreRangeMetrics(BaseModel):
    label: str
    minimum: int
    maximum: int
    suggestions: int
    pending: int
    accepted: int
    rejected: int
    acceptance_rate: float | None


class SiteEvaluationMetrics(BaseModel):
    site_id: int
    site_name: str
    suggestions: int
    pending: int
    accepted: int
    rejected: int
    applied: int
    acceptance_rate: float | None


class EvaluationTrendPoint(BaseModel):
    bucket_start: date
    generated: int
    accepted: int
    rejected: int
    applied: int
    acceptance_rate: float | None


class OrphanTrendPoint(BaseModel):
    snapshot_date: date
    active_articles: int
    remaining: int


class EvaluationComparison(BaseModel):
    previous_from: datetime
    previous_to: datetime
    suggestions_change_rate: float | None
    acceptance_rate_change: float | None
    placement_success_rate_change: float | None
    publication_success_rate_change: float | None


#: Sample states from the evidence plan, in the plan's own words.
EvidenceSampleState = Literal[
    "evidence_unavailable",
    "more_individual_labels_required",
    "three_site_baseline_ready",
]


class EvaluationProvenance(BaseModel):
    """What this dashboard is, where its numbers come from, and what they cannot settle.

    The evaluation page reports what the system did. That is not the same thing
    as evidence that the system should keep doing it: the rows are whatever
    editors happened to decide, with no held-out set, no frozen cohort and no
    agreed label quality. Shipping the numbers without saying so invites exactly
    the use the evidence plan forbids — pointing at a rate on this page to
    justify a ranking or model change.

    Every field here exists so a reader can tell how much weight the page bears
    before reading a single percentage.
    """

    #: Deliberately fixed. A future evidence surface gets its own value; a reader
    #: or a script must never have to guess which one it is looking at.
    surface: Literal["operational_telemetry"] = "operational_telemetry"
    schema_version: str
    #: The build these numbers were computed by, or None when the deployment does
    #: not record one. None is the honest answer, not a reason to omit the field.
    commit: str | None
    #: Newest suggestion included in the cohort. Nothing after this is counted.
    evidence_cutoff: datetime | None
    #: How the decisions were made, not how many there are.
    individual_labels: int
    bulk_labels: int
    exposed_individual_labels: int
    label_provenance: str
    sample_state: EvidenceSampleState
    #: Sites holding at least ``individual_label_target`` individual labels, and
    #: how many the three-site baseline asks for.
    sites_meeting_label_target: int
    individual_label_target: int
    baseline_site_target: int
    limitations: list[str]
    #: Always False for this surface. A ranking or model default may only move on
    #: a versioned artifact from the three-site baseline.
    supports_ranking_decisions: bool = False


class EvaluationMetricsOut(BaseModel):
    generated_at: datetime
    site_id: int | None
    date_from: datetime | None
    date_to: datetime | None
    cohort_definition: str
    provenance: EvaluationProvenance
    editorial: EditorialMetrics
    exposure: ExposureMetrics
    rejection_reasons: list[RejectionReasonMetric]
    graph_impact: GraphImpactMetrics
    placement: PlacementMetrics
    publication: PublicationMetrics
    orphans: OrphanMetrics
    comparison: EvaluationComparison | None
    trend: list[EvaluationTrendPoint]
    orphan_trend: list[OrphanTrendPoint]
    methods: list[MethodMetrics]
    score_ranges: list[ScoreRangeMetrics]
    sites: list[SiteEvaluationMetrics]


class EvaluationSuggestionOut(BaseModel):
    id: int
    trace_id: str
    site_id: int
    site_name: str
    source_title: str
    target_title: str
    method: str
    score: float
    status: str
    occurred_at: datetime


class EvaluationSuggestionPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EvaluationSuggestionOut]
