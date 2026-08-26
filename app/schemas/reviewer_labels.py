from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

ReviewerLabel = Literal["approved", "rejected"]
EvidenceSampleState = Literal[
    "evidence_unavailable",
    "more_individual_labels_required",
    "three_site_baseline_ready",
]
SplitMode = Literal["time", "site_holdout"]


class ReviewerLabelExampleOut(BaseModel):
    review_event_id: int
    suggestion_id: int
    trace_id: str
    site_id: int
    source_article_id: int
    target_article_id: int
    label: ReviewerLabel
    reviewed_at: datetime
    reviewer_id: str
    shown_at: datetime
    exposure_count: int
    method: str
    score: float
    retrieval_version: str
    ranking_version: str
    final_rank: int
    feature_snapshot: dict[str, Any]


class SiteLabelCountOut(BaseModel):
    site_id: int
    individual_labels: int
    exposed_individual_labels: int
    eligible_labels: int


class LabelReadinessOut(BaseModel):
    schema_version: int
    state: EvidenceSampleState
    ready: bool
    individual_labels: int
    bulk_labels: int
    exposed_individual_labels: int
    eligible_labels: int
    sites_meeting_label_target: int
    individual_label_target: int
    baseline_site_target: int
    qualifying_site_ids: list[int]
    site_counts: list[SiteLabelCountOut]
    excluded_unexposed: int
    excluded_missing_reviewer: int
    excluded_missing_exposure_timestamp: int
    excluded_external_targets: int
    excluded_incomplete_snapshots: int
    blocked_reasons: list[str]


class ReviewerLabelSplitOut(BaseModel):
    schema_version: int
    split_mode: SplitMode
    cutoff_at: datetime | None
    holdout_site_id: int | None
    train: list[ReviewerLabelExampleOut]
    test: list[ReviewerLabelExampleOut]
    train_site_ids: list[int]
    test_site_ids: list[int]
    site_overlap: list[int]


class ReviewerLabelDatasetOut(BaseModel):
    schema_version: int
    cutoff_at: datetime
    holdout_site_id: int | None
    readiness: LabelReadinessOut
    labels: list[ReviewerLabelExampleOut]
    time_split: ReviewerLabelSplitOut
    site_holdout_split: ReviewerLabelSplitOut | None
