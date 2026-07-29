"""Publication counter arithmetic, exercised directly rather than through a job run.

The integration tests in test_publication.py drive these transitions from freshly
created rows, which never reaches the defensive paths: progress written before
`attempt_failures` existed, and values a hand-edited or partially written JSONB row
can hold. Both are covered here, along with the invariant every terminal run must
satisfy — applied + failed + skipped accounts for exactly the original total.
"""

import pytest

from app.services.publication_progress import (
    begin_publication_attempt,
    complete_publication_success,
    mark_publication_failure,
    record_publication_applied,
    record_publication_failure,
    record_publication_skip,
)


def _publishing(**fields) -> dict:
    return {"stage": "publishing", **fields}


def test_first_attempt_starts_from_the_batch():
    assert begin_publication_attempt(None, 10) == {
        "stage": "publishing",
        "applied": 0,
        "failed": 0,
        "skipped": 0,
        "total": 10,
        "attempt_skipped": 0,
        "attempt_failed": 0,
        "attempt_failures": 0,
        "failure_state": None,
    }


@pytest.mark.parametrize(
    "previous",
    [None, {}, {"stage": "analyzing", "applied": 5, "total": 99}],
    ids=["missing", "empty", "another-stage"],
)
def test_progress_from_another_stage_never_seeds_the_batch(previous):
    started = begin_publication_attempt(previous, 3)

    assert started["applied"] == 0
    assert started["total"] == 3


def test_retry_keeps_the_original_denominator():
    previous = _publishing(applied=9, failed=1, skipped=0, total=10, attempt_failures=1)

    started = begin_publication_attempt(previous, 1)

    assert started["total"] == 10
    assert started["applied"] == 9
    assert started["attempt_failures"] == 1
    # Per-attempt diagnostics reset; the cumulative event count does not.
    assert started["failed"] == 0
    assert started["attempt_failed"] == 0
    assert started["attempt_skipped"] == 0
    assert started["failure_state"] is None


def test_approvals_between_attempts_expand_the_denominator():
    previous = _publishing(applied=9, total=10, attempt_failures=1)

    # One rolled-back failure plus five suggestions approved during the retry wait.
    assert begin_publication_attempt(previous, 6)["total"] == 15


def test_legacy_rows_seed_the_event_count_from_failed():
    # Written before attempt_failures existed, when `failed` was the only counter.
    previous = _publishing(applied=2, failed=3, skipped=0, total=5)

    started = begin_publication_attempt(previous, 3)

    assert started["attempt_failures"] == 3
    assert started["failed"] == 0


@pytest.mark.parametrize(
    "corrupt",
    [True, -4, "12", None, 1.5],
    ids=["bool", "negative", "string", "none", "float"],
)
def test_unusable_stored_counts_fall_back_to_zero(corrupt):
    previous = _publishing(applied=corrupt, skipped=corrupt, total=corrupt)

    started = begin_publication_attempt(previous, 2)

    assert started["applied"] == 0
    assert started["skipped"] == 0
    assert started["total"] == 2


def test_applied_increments_without_disturbing_other_counters():
    progress = begin_publication_attempt(None, 3)

    progress = record_publication_applied(progress)

    assert progress["applied"] == 1
    assert progress["total"] == 3
    assert progress["skipped"] == 0
    assert progress["attempt_failed"] == 0


def test_skips_do_not_recount_items_carried_from_an_earlier_attempt():
    # Two suggestions were skipped before; both can be skipped again if a reviewer
    # reactivated their articles, so the cumulative count must not simply add up.
    progress = begin_publication_attempt(_publishing(skipped=2, total=5), 3)

    progress = record_publication_skip(progress)
    assert (progress["skipped"], progress["attempt_skipped"]) == (2, 1)

    progress = record_publication_skip(progress)
    assert (progress["skipped"], progress["attempt_skipped"]) == (2, 2)

    # A third skip exceeds what the previous attempt recorded, so it is genuinely new.
    progress = record_publication_skip(progress)
    assert (progress["skipped"], progress["attempt_skipped"]) == (3, 3)


def test_failures_track_the_attempt_while_events_accumulate():
    progress = begin_publication_attempt(_publishing(total=4, attempt_failures=1), 4)

    progress = record_publication_failure(progress)
    assert (progress["failed"], progress["attempt_failed"]) == (1, 1)
    assert progress["attempt_failures"] == 2

    progress = record_publication_failure(progress)
    assert (progress["failed"], progress["attempt_failed"]) == (2, 2)
    assert progress["attempt_failures"] == 3


def test_success_clears_failure_state_and_derives_the_skipped_remainder():
    progress = _publishing(
        applied=7,
        failed=2,
        skipped=1,
        total=10,
        attempt_skipped=1,
        attempt_failed=2,
        attempt_failures=2,
        failure_state="retrying",
    )

    completed = complete_publication_success(progress)

    assert completed["failed"] == 0
    assert completed["attempt_failed"] == 0
    assert completed["failure_state"] is None
    # Nothing failed this attempt, so every item not applied was skipped — including
    # the ones an earlier attempt could not attribute.
    assert completed["skipped"] == 3
    assert completed["attempt_failures"] == 2


def test_success_never_reports_more_applied_than_total():
    completed = complete_publication_success(_publishing(applied=15, total=10))

    assert completed["total"] == 15
    assert completed["skipped"] == 0


@pytest.mark.parametrize(
    "progress",
    [None, {}, {"stage": "analyzing", "applied": 1}, "not-a-dict"],
    ids=["missing", "empty", "another-stage", "wrong-type"],
)
def test_only_publication_progress_is_reconciled(progress):
    assert mark_publication_failure(progress, terminal=False) is None
    assert mark_publication_failure(progress, terminal=True) is None


def test_retrying_run_reports_the_latest_attempt_failures():
    progress = _publishing(
        applied=9, failed=0, skipped=0, total=10, attempt_failed=1, attempt_failures=1
    )

    marked = mark_publication_failure(progress, terminal=False)

    assert marked["failed"] == 1
    assert marked["failure_state"] == "retrying"
    # A retry is still coming, so the denominator and successes stay untouched.
    assert (marked["applied"], marked["total"]) == (9, 10)


def test_terminal_run_counts_entries_the_loop_never_reached():
    # A work horse killed after two commits: attempt_failed is 0 because nothing
    # raised, yet eight items will never be applied.
    progress = _publishing(applied=2, failed=0, skipped=0, total=10, attempt_failed=0)

    marked = mark_publication_failure(progress, terminal=True)

    assert marked["failed"] == 8
    assert marked["skipped"] == 0
    assert marked["failure_state"] == "terminal"


def test_terminal_run_treats_the_attempt_count_as_a_floor():
    # Three raised this attempt but the stored skip count leaves room for only one,
    # so the skip total is the unreliable figure and gets reconciled down.
    progress = _publishing(applied=5, failed=0, skipped=4, total=10, attempt_failed=3)

    marked = mark_publication_failure(progress, terminal=True)

    assert marked["failed"] == 3
    assert marked["skipped"] == 2


@pytest.mark.parametrize(
    "progress",
    [
        _publishing(applied=2, skipped=0, total=10, attempt_failed=0),
        _publishing(applied=5, skipped=4, total=10, attempt_failed=3),
        _publishing(applied=0, skipped=0, total=1, attempt_failed=1),
        _publishing(applied=10, skipped=0, total=10, attempt_failed=0),
        _publishing(applied=0, skipped=3, total=3, attempt_failed=0),
    ],
    ids=["killed-early", "floor-applies", "single-item", "all-applied", "all-skipped"],
)
def test_terminal_counters_always_account_for_the_whole_total(progress):
    marked = mark_publication_failure(progress, terminal=True)

    assert marked["applied"] + marked["failed"] + marked["skipped"] == marked["total"]
