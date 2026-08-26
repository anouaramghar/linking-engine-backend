from dataclasses import dataclass
from math import floor
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ml.hybrid import RankedCandidate
from app.models import Site, Suggestion

ACCEPTED_STATUSES = {"approved", "applying", "applied", "failed"}
DECIDED_STATUSES = (*ACCEPTED_STATUSES, "rejected")
FEEDBACK_CANDIDATE_POOL = 20


def score_percent(score: float) -> int:
    return max(0, min(100, floor(score * 100 + 0.5)))


def score_bucket(percent: int) -> tuple[int, int]:
    if percent < 60:
        return 0, 59
    if percent < 70:
        return 60, 69
    if percent < 80:
        return 70, 79
    if percent < 90:
        return 80, 89
    return 90, 100


@dataclass(frozen=True)
class FeedbackBucket:
    minimum: int
    maximum: int
    accepted: int
    rejected: int

    @property
    def samples(self) -> int:
        return self.accepted + self.rejected


@dataclass(frozen=True)
class EditorialFeedbackProfile:
    site_id: int
    accepted: int
    rejected: int
    buckets: dict[tuple[int, int], FeedbackBucket]

    @property
    def samples(self) -> int:
        return self.accepted + self.rejected

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.samples if self.samples else 0.5


def load_editorial_feedback(
    db: Session,
    site: Site,
) -> EditorialFeedbackProfile | None:
    """This site's accept/reject history, or None when it must not steer ranking.

    Off unless an operator switched it on for this site. The sample gate below is
    a floor, not proof: it counts decided rows, and a bulk "reject everything
    under 70" writes the same rows as ten considered ones. Nothing here has been
    measured against a held-out set, so the feature stays opt-in per site until
    the three-site baseline in the evidence plan exists.
    """
    if not site.editorial_feedback_enabled or site.editorial_feedback_weight <= 0:
        return None
    rows = list(
        db.execute(
            select(Suggestion.score, Suggestion.status).where(
                Suggestion.site_id == site.id,
                Suggestion.status.in_(DECIDED_STATUSES),
            )
        )
    )
    if len(rows) < site.editorial_feedback_min_samples:
        return None
    counts: dict[tuple[int, int], list[int]] = {}
    accepted_total = 0
    rejected_total = 0
    for score, status in rows:
        bucket = score_bucket(score_percent(score))
        values = counts.setdefault(bucket, [0, 0])
        if status in ACCEPTED_STATUSES:
            values[0] += 1
            accepted_total += 1
        else:
            values[1] += 1
            rejected_total += 1
    return EditorialFeedbackProfile(
        site_id=site.id,
        accepted=accepted_total,
        rejected=rejected_total,
        buckets={
            key: FeedbackBucket(key[0], key[1], values[0], values[1])
            for key, values in counts.items()
        },
    )


def _smoothed_acceptance(
    profile: EditorialFeedbackProfile,
    candidate: RankedCandidate,
) -> tuple[float, FeedbackBucket | None]:
    bucket = profile.buckets.get(score_bucket(score_percent(candidate.semantic_score)))
    # Five prior observations keep a tiny bucket from overturning the original
    # rank because of one accept/reject.
    prior_strength = 5
    accepted = bucket.accepted if bucket else 0
    samples = bucket.samples if bucket else 0
    value = (accepted + profile.acceptance_rate * prior_strength) / (samples + prior_strength)
    return value, bucket


def rerank_with_editorial_feedback(
    candidates: Sequence[RankedCandidate],
    profile: EditorialFeedbackProfile,
    *,
    weight: float,
) -> tuple[list[RankedCandidate], dict[int, dict]]:
    if not candidates:
        return [], {}
    denominator = max(1, len(candidates) - 1)
    scored = []
    for original_index, candidate in enumerate(candidates):
        acceptance, bucket = _smoothed_acceptance(profile, candidate)
        original_utility = 1 - original_index / denominator
        combined = (1 - weight) * original_utility + weight * acceptance
        scored.append((candidate, original_index, combined, acceptance, bucket))
    scored.sort(key=lambda row: (-row[2], row[1], row[0].target_id))

    components: dict[int, dict] = {}
    ordered: list[RankedCandidate] = []
    for feedback_index, (candidate, original_index, combined, acceptance, bucket) in enumerate(
        scored
    ):
        ordered.append(candidate)
        components[candidate.target_id] = {
            "version": "editorial_feedback_v1",
            "site_samples": profile.samples,
            "site_acceptance_rate": round(profile.acceptance_rate, 6),
            "score_bucket": (
                f"{bucket.minimum}-{bucket.maximum}"
                if bucket is not None
                else f"{score_bucket(score_percent(candidate.semantic_score))[0]}-"
                f"{score_bucket(score_percent(candidate.semantic_score))[1]}"
            ),
            "bucket_samples": bucket.samples if bucket else 0,
            "smoothed_acceptance_rate": round(acceptance, 6),
            "weight": weight,
            "original_rank": original_index + 1,
            "feedback_rank": feedback_index + 1,
            "combined_score": round(combined, 6),
        }
    return ordered, components
