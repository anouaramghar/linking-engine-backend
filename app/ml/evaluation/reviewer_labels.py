"""Evidence-gated reviewer labels for offline ranking evaluation.

The operational evaluation page can count every decision, but a ranking
benchmark needs a narrower contract. This module is that contract:

* only immutable ``reviewed`` events marked as individual and exposed count;
* only internal suggestions with a complete Slice 4 ranking snapshot count;
* a frozen artifact refuses to build until three sites each have 100 eligible
  labels; and
* time and site splits are deterministic and carry their site membership so a
  comparison cannot silently change cohorts.

The module deliberately stops at a dataset boundary. It does not fit, promote,
rollback, or activate a learned ranker.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Suggestion, SuggestionEvent

Label = Literal["approved", "rejected"]
SplitMode = Literal["time", "site_holdout"]
EvidenceSampleState = Literal[
    "evidence_unavailable",
    "more_individual_labels_required",
    "three_site_baseline_ready",
]

SCHEMA_VERSION = 1
INDIVIDUAL_LABEL_TARGET = 100
BASELINE_SITE_TARGET = 3
DECISION_LABELS = frozenset({"approved", "rejected"})


class FrozenReviewerLabelError(ValueError):
    """A frozen reviewer-label artifact cannot be read as this schema."""


class LabelReadinessError(RuntimeError):
    """Raised when a training/evaluation artifact is requested too early."""

    def __init__(self, readiness: "LabelReadiness") -> None:
        self.readiness = readiness
        super().__init__(
            "reviewer-label evidence is not ready: " + "; ".join(readiness.blocked_reasons)
        )


def _parse_datetime(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise FrozenReviewerLabelError(f"{field} is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FrozenReviewerLabelError(f"{field} must include a timezone")
    return parsed


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


@dataclass(frozen=True)
class ReviewerLabelExample:
    """One immutable, exposed, individual reviewer decision."""

    review_event_id: int
    suggestion_id: int
    trace_id: str
    site_id: int
    source_article_id: int
    target_article_id: int
    label: Label
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

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reviewed_at"] = self.reviewed_at.isoformat()
        payload["shown_at"] = self.shown_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewerLabelExample":
        label = payload.get("label")
        if label not in DECISION_LABELS:
            raise FrozenReviewerLabelError(f"unsupported reviewer label {label!r}")
        snapshot = payload.get("feature_snapshot")
        if not isinstance(snapshot, dict):
            raise FrozenReviewerLabelError("feature_snapshot must be an object")
        return cls(
            review_event_id=int(payload["review_event_id"]),
            suggestion_id=int(payload["suggestion_id"]),
            trace_id=str(payload["trace_id"]),
            site_id=int(payload["site_id"]),
            source_article_id=int(payload["source_article_id"]),
            target_article_id=int(payload["target_article_id"]),
            label=label,
            reviewed_at=_parse_datetime(payload["reviewed_at"], field="reviewed_at"),
            reviewer_id=str(payload["reviewer_id"]),
            shown_at=_parse_datetime(payload["shown_at"], field="shown_at"),
            exposure_count=int(payload["exposure_count"]),
            method=str(payload["method"]),
            score=float(payload["score"]),
            retrieval_version=str(payload["retrieval_version"]),
            ranking_version=str(payload["ranking_version"]),
            final_rank=int(payload["final_rank"]),
            feature_snapshot=snapshot,
        )


@dataclass(frozen=True)
class SiteLabelCount:
    site_id: int
    individual_labels: int
    exposed_individual_labels: int
    eligible_labels: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SiteLabelCount":
        return cls(
            site_id=int(payload["site_id"]),
            individual_labels=int(payload["individual_labels"]),
            exposed_individual_labels=int(payload["exposed_individual_labels"]),
            eligible_labels=int(payload["eligible_labels"]),
        )


@dataclass(frozen=True)
class LabelReadiness:
    """The only evidence state a future learner is allowed to consume."""

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
    qualifying_site_ids: tuple[int, ...]
    site_counts: tuple[SiteLabelCount, ...]
    excluded_unexposed: int
    excluded_missing_reviewer: int
    excluded_missing_exposure_timestamp: int
    excluded_external_targets: int
    excluded_incomplete_snapshots: int
    blocked_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "ready": self.ready,
            "individual_labels": self.individual_labels,
            "bulk_labels": self.bulk_labels,
            "exposed_individual_labels": self.exposed_individual_labels,
            "eligible_labels": self.eligible_labels,
            "sites_meeting_label_target": self.sites_meeting_label_target,
            "individual_label_target": self.individual_label_target,
            "baseline_site_target": self.baseline_site_target,
            "qualifying_site_ids": list(self.qualifying_site_ids),
            "site_counts": [item.to_dict() for item in self.site_counts],
            "excluded_unexposed": self.excluded_unexposed,
            "excluded_missing_reviewer": self.excluded_missing_reviewer,
            "excluded_missing_exposure_timestamp": self.excluded_missing_exposure_timestamp,
            "excluded_external_targets": self.excluded_external_targets,
            "excluded_incomplete_snapshots": self.excluded_incomplete_snapshots,
            "blocked_reasons": list(self.blocked_reasons),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LabelReadiness":
        if int(payload.get("schema_version", -1)) != SCHEMA_VERSION:
            raise FrozenReviewerLabelError(
                f"readiness has schema_version {payload.get('schema_version')!r}, "
                f"this code reads {SCHEMA_VERSION}"
            )
        state = payload.get("state")
        if state not in {
            "evidence_unavailable",
            "more_individual_labels_required",
            "three_site_baseline_ready",
        }:
            raise FrozenReviewerLabelError(f"unsupported readiness state {state!r}")
        return cls(
            schema_version=int(payload["schema_version"]),
            state=state,
            ready=bool(payload["ready"]),
            individual_labels=int(payload["individual_labels"]),
            bulk_labels=int(payload["bulk_labels"]),
            exposed_individual_labels=int(payload["exposed_individual_labels"]),
            eligible_labels=int(payload["eligible_labels"]),
            sites_meeting_label_target=int(payload["sites_meeting_label_target"]),
            individual_label_target=int(payload["individual_label_target"]),
            baseline_site_target=int(payload["baseline_site_target"]),
            qualifying_site_ids=tuple(int(value) for value in payload["qualifying_site_ids"]),
            site_counts=tuple(SiteLabelCount.from_dict(item) for item in payload["site_counts"]),
            excluded_unexposed=int(payload["excluded_unexposed"]),
            excluded_missing_reviewer=int(payload["excluded_missing_reviewer"]),
            excluded_missing_exposure_timestamp=int(payload["excluded_missing_exposure_timestamp"]),
            excluded_external_targets=int(payload["excluded_external_targets"]),
            excluded_incomplete_snapshots=int(payload["excluded_incomplete_snapshots"]),
            blocked_reasons=tuple(str(value) for value in payload["blocked_reasons"]),
        )


@dataclass(frozen=True)
class ReviewerLabelSplit:
    schema_version: int
    split_mode: SplitMode
    cutoff_at: datetime | None
    holdout_site_id: int | None
    train: tuple[ReviewerLabelExample, ...]
    test: tuple[ReviewerLabelExample, ...]
    train_site_ids: tuple[int, ...]
    test_site_ids: tuple[int, ...]
    site_overlap: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "split_mode": self.split_mode,
            "cutoff_at": self.cutoff_at.isoformat() if self.cutoff_at else None,
            "holdout_site_id": self.holdout_site_id,
            "train": [row.to_dict() for row in self.train],
            "test": [row.to_dict() for row in self.test],
            "train_site_ids": list(self.train_site_ids),
            "test_site_ids": list(self.test_site_ids),
            "site_overlap": list(self.site_overlap),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewerLabelSplit":
        if int(payload.get("schema_version", -1)) != SCHEMA_VERSION:
            raise FrozenReviewerLabelError(
                f"reviewer-label split has schema_version {payload.get('schema_version')!r}, "
                f"this code reads {SCHEMA_VERSION}"
            )
        mode = payload.get("split_mode")
        if mode not in {"time", "site_holdout"}:
            raise FrozenReviewerLabelError(f"unsupported split mode {mode!r}")
        cutoff = payload.get("cutoff_at")
        if mode == "time" and not cutoff:
            raise FrozenReviewerLabelError("time split must have cutoff_at")
        if mode == "site_holdout" and payload.get("holdout_site_id") is None:
            raise FrozenReviewerLabelError("site holdout split must have holdout_site_id")
        return cls(
            schema_version=int(payload["schema_version"]),
            split_mode=mode,
            cutoff_at=_parse_datetime(cutoff, field="cutoff_at") if cutoff else None,
            holdout_site_id=(
                int(payload["holdout_site_id"])
                if payload.get("holdout_site_id") is not None
                else None
            ),
            train=tuple(ReviewerLabelExample.from_dict(row) for row in payload["train"]),
            test=tuple(ReviewerLabelExample.from_dict(row) for row in payload["test"]),
            train_site_ids=tuple(int(value) for value in payload["train_site_ids"]),
            test_site_ids=tuple(int(value) for value in payload["test_site_ids"]),
            site_overlap=tuple(int(value) for value in payload["site_overlap"]),
        )


@dataclass(frozen=True)
class ReviewerLabelDataset:
    schema_version: int
    cutoff_at: datetime
    holdout_site_id: int | None
    readiness: LabelReadiness
    labels: tuple[ReviewerLabelExample, ...]
    time_split: ReviewerLabelSplit
    site_holdout_split: ReviewerLabelSplit | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cutoff_at": self.cutoff_at.isoformat(),
            "holdout_site_id": self.holdout_site_id,
            "readiness": self.readiness.to_dict(),
            "labels": [row.to_dict() for row in self.labels],
            "time_split": self.time_split.to_dict(),
            "site_holdout_split": (
                self.site_holdout_split.to_dict() if self.site_holdout_split else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewerLabelDataset":
        if int(payload.get("schema_version", -1)) != SCHEMA_VERSION:
            raise FrozenReviewerLabelError(
                f"reviewer-label dataset has schema_version {payload.get('schema_version')!r}, "
                f"this code reads {SCHEMA_VERSION}"
            )
        site_split = payload.get("site_holdout_split")
        cutoff_at = _parse_datetime(payload["cutoff_at"], field="cutoff_at")
        time_split = ReviewerLabelSplit.from_dict(payload["time_split"])
        if time_split.split_mode != "time" or time_split.cutoff_at != cutoff_at:
            raise FrozenReviewerLabelError("time split does not match the dataset cutoff")
        if site_split is not None:
            parsed_site_split = ReviewerLabelSplit.from_dict(site_split)
            if parsed_site_split.holdout_site_id != payload.get("holdout_site_id"):
                raise FrozenReviewerLabelError(
                    "site holdout split does not match the dataset holdout site"
                )
        else:
            parsed_site_split = None
        return cls(
            schema_version=SCHEMA_VERSION,
            cutoff_at=cutoff_at,
            holdout_site_id=(
                int(payload["holdout_site_id"])
                if payload.get("holdout_site_id") is not None
                else None
            ),
            readiness=LabelReadiness.from_dict(payload["readiness"]),
            labels=tuple(ReviewerLabelExample.from_dict(row) for row in payload["labels"]),
            time_split=time_split,
            site_holdout_split=parsed_site_split,
        )


@dataclass
class _MutableSiteCount:
    individual_labels: int = 0
    exposed_individual_labels: int = 0
    eligible_labels: int = 0


@dataclass
class _CollectedLabels:
    labels: list[ReviewerLabelExample]
    individual_labels: int = 0
    bulk_labels: int = 0
    exposed_individual_labels: int = 0
    excluded_unexposed: int = 0
    excluded_missing_reviewer: int = 0
    excluded_missing_exposure_timestamp: int = 0
    excluded_external_targets: int = 0
    excluded_incomplete_snapshots: int = 0
    site_counts: dict[int, _MutableSiteCount] | None = None


def _review_rows(
    db: Session,
    *,
    site_ids: tuple[int, ...] | None,
    date_from: datetime | None,
    date_to: datetime | None,
):
    conditions = [
        SuggestionEvent.event_type == "reviewed",
        SuggestionEvent.details["to_status"].as_string().in_(tuple(DECISION_LABELS)),
    ]
    if site_ids:
        conditions.append(Suggestion.site_id.in_(site_ids))
    if date_from is not None:
        _require_aware(date_from, "date_from")
        conditions.append(SuggestionEvent.created_at >= date_from)
    if date_to is not None:
        _require_aware(date_to, "date_to")
        conditions.append(SuggestionEvent.created_at < date_to)
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise ValueError("date_from must be before date_to")
    return db.execute(
        select(SuggestionEvent, Suggestion)
        .join(Suggestion, Suggestion.id == SuggestionEvent.suggestion_id)
        .where(*conditions)
        .order_by(SuggestionEvent.created_at, SuggestionEvent.id)
    ).all()


def _complete_example(
    event: SuggestionEvent, suggestion: Suggestion
) -> ReviewerLabelExample | None:
    details = event.details if isinstance(event.details, dict) else {}
    if details.get("review_kind") != "individual":
        return None
    label = details.get("to_status")
    if label not in DECISION_LABELS:
        return None
    if details.get("exposed") is not True:
        return None
    reviewer_id = details.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        return None
    if suggestion.target_article_id is None or suggestion.shown_at is None:
        return None
    if event.created_at is None:
        return None
    snapshot_values = (
        suggestion.retrieval_version,
        suggestion.ranking_version,
        suggestion.final_rank,
        suggestion.feature_snapshot,
    )
    if any(value is None for value in snapshot_values):
        return None
    if not isinstance(suggestion.feature_snapshot, dict):
        return None
    return ReviewerLabelExample(
        review_event_id=event.id,
        suggestion_id=suggestion.id,
        trace_id=suggestion.trace_id,
        site_id=suggestion.site_id,
        source_article_id=suggestion.source_article_id,
        target_article_id=suggestion.target_article_id,
        label=label,
        reviewed_at=event.created_at,
        reviewer_id=reviewer_id,
        shown_at=suggestion.shown_at,
        exposure_count=suggestion.exposure_count,
        method=suggestion.method,
        score=suggestion.score,
        retrieval_version=suggestion.retrieval_version,
        ranking_version=suggestion.ranking_version,
        final_rank=suggestion.final_rank,
        feature_snapshot=dict(suggestion.feature_snapshot),
    )


def _collect_labels(
    db: Session,
    *,
    site_ids: tuple[int, ...] | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> _CollectedLabels:
    collected = _CollectedLabels(labels=[], site_counts=defaultdict(_MutableSiteCount))
    for event, suggestion in _review_rows(
        db,
        site_ids=site_ids,
        date_from=date_from,
        date_to=date_to,
    ):
        details = event.details if isinstance(event.details, dict) else {}
        review_kind = details.get("review_kind")
        if review_kind == "bulk":
            collected.bulk_labels += 1
            continue
        if review_kind != "individual" or details.get("to_status") not in DECISION_LABELS:
            continue

        collected.individual_labels += 1
        site = collected.site_counts[suggestion.site_id]
        site.individual_labels += 1
        if details.get("exposed") is True:
            collected.exposed_individual_labels += 1
            site.exposed_individual_labels += 1
        else:
            collected.excluded_unexposed += 1
            continue

        reviewer_id = details.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            collected.excluded_missing_reviewer += 1
            continue
        if suggestion.shown_at is None:
            collected.excluded_missing_exposure_timestamp += 1
            continue
        if suggestion.target_article_id is None:
            collected.excluded_external_targets += 1
            continue
        if any(
            value is None
            for value in (
                suggestion.retrieval_version,
                suggestion.ranking_version,
                suggestion.final_rank,
                suggestion.feature_snapshot,
            )
        ) or not isinstance(suggestion.feature_snapshot, dict):
            collected.excluded_incomplete_snapshots += 1
            continue
        example = _complete_example(event, suggestion)
        if example is not None:
            collected.labels.append(example)
            site.eligible_labels += 1
    return collected


def _readiness(collected: _CollectedLabels) -> LabelReadiness:
    site_counts = tuple(
        SiteLabelCount(
            site_id=site_id,
            individual_labels=counts.individual_labels,
            exposed_individual_labels=counts.exposed_individual_labels,
            eligible_labels=counts.eligible_labels,
        )
        for site_id, counts in sorted((collected.site_counts or {}).items())
    )
    qualifying = tuple(
        item.site_id for item in site_counts if item.eligible_labels >= INDIVIDUAL_LABEL_TARGET
    )
    ready = len(qualifying) >= BASELINE_SITE_TARGET
    if collected.individual_labels == 0:
        state: EvidenceSampleState = "evidence_unavailable"
    elif ready:
        state = "three_site_baseline_ready"
    else:
        state = "more_individual_labels_required"
    blocked: list[str] = []
    if not ready:
        blocked.append(
            "requires three representative sites with at least 100 eligible exposed "
            "individual labels each"
        )
    if collected.excluded_unexposed:
        blocked.append("unseen individual decisions are excluded")
    if collected.excluded_missing_reviewer:
        blocked.append("individual decisions without reviewer identity are excluded")
    if collected.excluded_incomplete_snapshots:
        blocked.append(
            "individual decisions without complete immutable ranking snapshots are excluded"
        )
    if not blocked:
        blocked.append("evidence gate passed")
    return LabelReadiness(
        schema_version=SCHEMA_VERSION,
        state=state,
        ready=ready,
        individual_labels=collected.individual_labels,
        bulk_labels=collected.bulk_labels,
        exposed_individual_labels=collected.exposed_individual_labels,
        eligible_labels=len(collected.labels),
        sites_meeting_label_target=len(qualifying),
        individual_label_target=INDIVIDUAL_LABEL_TARGET,
        baseline_site_target=BASELINE_SITE_TARGET,
        qualifying_site_ids=qualifying,
        site_counts=site_counts,
        excluded_unexposed=collected.excluded_unexposed,
        excluded_missing_reviewer=collected.excluded_missing_reviewer,
        excluded_missing_exposure_timestamp=collected.excluded_missing_exposure_timestamp,
        excluded_external_targets=collected.excluded_external_targets,
        excluded_incomplete_snapshots=collected.excluded_incomplete_snapshots,
        blocked_reasons=tuple(blocked),
    )


def inspect_label_readiness(
    db: Session,
    *,
    site_ids: tuple[int, ...] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> LabelReadiness:
    """Report the evidence gate without creating an evaluation artifact."""
    return _readiness(
        _collect_labels(
            db,
            site_ids=site_ids,
            date_from=date_from,
            date_to=date_to,
        )
    )


def eligible_reviewer_labels(
    db: Session,
    *,
    site_ids: tuple[int, ...] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[ReviewerLabelExample, ...]:
    """Return only exposed, individual, snapshot-complete internal decisions."""
    return tuple(
        _collect_labels(
            db,
            site_ids=site_ids,
            date_from=date_from,
            date_to=date_to,
        ).labels
    )


def _latest_per_suggestion(
    labels: Sequence[ReviewerLabelExample],
) -> tuple[ReviewerLabelExample, ...]:
    latest: dict[int, ReviewerLabelExample] = {}
    for row in labels:
        previous = latest.get(row.suggestion_id)
        if previous is None or (row.reviewed_at, row.review_event_id) > (
            previous.reviewed_at,
            previous.review_event_id,
        ):
            latest[row.suggestion_id] = row
    return tuple(sorted(latest.values(), key=lambda row: (row.reviewed_at, row.review_event_id)))


def _split(
    *,
    mode: SplitMode,
    train: Sequence[ReviewerLabelExample],
    test: Sequence[ReviewerLabelExample],
    cutoff_at: datetime | None,
    holdout_site_id: int | None,
) -> ReviewerLabelSplit:
    train_rows = tuple(train)
    test_rows = tuple(test)
    train_sites = tuple(sorted({row.site_id for row in train_rows}))
    test_sites = tuple(sorted({row.site_id for row in test_rows}))
    return ReviewerLabelSplit(
        schema_version=SCHEMA_VERSION,
        split_mode=mode,
        cutoff_at=cutoff_at,
        holdout_site_id=holdout_site_id,
        train=train_rows,
        test=test_rows,
        train_site_ids=train_sites,
        test_site_ids=test_sites,
        site_overlap=tuple(sorted(set(train_sites) & set(test_sites))),
    )


def build_time_split(
    labels: Sequence[ReviewerLabelExample], *, cutoff_at: datetime
) -> ReviewerLabelSplit:
    """Split by immutable review time, keeping one latest label per suggestion."""
    _require_aware(cutoff_at, "cutoff_at")
    frozen_rows = _latest_per_suggestion(labels)
    return _split(
        mode="time",
        train=tuple(row for row in frozen_rows if row.reviewed_at < cutoff_at),
        test=tuple(row for row in frozen_rows if row.reviewed_at >= cutoff_at),
        cutoff_at=cutoff_at,
        holdout_site_id=None,
    )


def build_site_holdout_split(
    labels: Sequence[ReviewerLabelExample], *, holdout_site_id: int
) -> ReviewerLabelSplit:
    """Hold out every label from one named site, with no site overlap."""
    if holdout_site_id < 1:
        raise ValueError("holdout_site_id must be positive")
    frozen_rows = _latest_per_suggestion(labels)
    if not any(row.site_id == holdout_site_id for row in frozen_rows):
        raise ValueError(f"holdout site {holdout_site_id} has no eligible labels")
    return _split(
        mode="site_holdout",
        train=tuple(row for row in frozen_rows if row.site_id != holdout_site_id),
        test=tuple(row for row in frozen_rows if row.site_id == holdout_site_id),
        cutoff_at=None,
        holdout_site_id=holdout_site_id,
    )


def build_reviewer_label_dataset(
    db: Session,
    *,
    cutoff_at: datetime,
    site_ids: tuple[int, ...] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    holdout_site_id: int | None = None,
    require_ready: bool = True,
) -> ReviewerLabelDataset:
    """Build a reproducible dataset, refusing to make a training artifact early.

    ``require_ready=False`` is reserved for the admin evidence export. It still
    returns only eligible rows, but marks the embedded readiness state false; the
    freeze script and any future learner use the default fail-closed behavior.
    """
    _require_aware(cutoff_at, "cutoff_at")
    collected = _collect_labels(
        db,
        site_ids=site_ids,
        date_from=date_from,
        date_to=date_to,
    )
    readiness = _readiness(collected)
    if require_ready and not readiness.ready:
        raise LabelReadinessError(readiness)
    labels = tuple(collected.labels)
    time_split = build_time_split(labels, cutoff_at=cutoff_at)
    site_split = (
        build_site_holdout_split(labels, holdout_site_id=holdout_site_id)
        if holdout_site_id is not None
        else None
    )
    return ReviewerLabelDataset(
        schema_version=SCHEMA_VERSION,
        cutoff_at=cutoff_at,
        holdout_site_id=holdout_site_id,
        readiness=readiness,
        labels=labels,
        time_split=time_split,
        site_holdout_split=site_split,
    )
