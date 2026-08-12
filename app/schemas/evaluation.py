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


class EvaluationMetricsOut(BaseModel):
    generated_at: datetime
    site_id: int | None
    date_from: datetime | None
    date_to: datetime | None
    cohort_definition: str
    editorial: EditorialMetrics
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
