"""Publication progress accounting shared by the task and durable job lifecycle.

`applied` is a monotonic count of committed suggestion updates. `failed` keeps its
original caller-facing meaning: failures still unresolved in the latest attempt.
A terminal run widens it to every original item neither applied nor skipped, because
a killed work horse leaves entries the loop never reached and no later attempt will.
`attempt_failures` is the monotonic event count across attempts, so a transient
failure that later applies is not also reported as a terminally failed item.

The original total remains stable for an ordinary retry, but approvals added between
attempts expand it enough that committed applications can never exceed it. Counts do
not retain suggestion identities, so repeated skips are de-duplicated conservatively
during an attempt and reconciled to internally consistent final counters when the run
succeeds or becomes terminal.
"""

_STAGE = "publishing"

#: Connector outcomes carried in progress next to `applied`, and resumed for the
#: same reason: the row that produced one was committed, so restarting the count
#: on a retry would report "applied 10, inserted 1" for the same ten links.
PUBLICATION_OUTCOMES = ("inserted", "block", "already_present")


def _count(progress: dict, key: str, default: int = 0) -> int:
    value = progress.get(key, default)
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
    )


def begin_publication_attempt(previous: dict | None, batch_size: int) -> dict:
    previous = previous if isinstance(previous, dict) and previous.get("stage") == _STAGE else {}
    applied = _count(previous, "applied")
    # On older rows `failed` was the only failure counter. Seeding the event counter
    # from it preserves that history while `failed` resumes its per-attempt meaning.
    attempt_failures = _count(
        previous,
        "attempt_failures",
        _count(previous, "failed"),
    )
    return {
        "stage": _STAGE,
        "applied": applied,
        "failed": 0,
        "skipped": _count(previous, "skipped"),
        # `applied + batch_size` is the maximum number this attempt can commit.
        # Taking the larger value keeps a retry's original denominator but admits
        # suggestions approved while RQ was waiting to retry.
        "total": max(_count(previous, "total"), applied + batch_size),
        "attempt_skipped": 0,
        "attempt_failed": 0,
        "attempt_failures": attempt_failures,
        "failure_state": None,
    }


def resume_outcome_counts(previous: dict | None) -> dict[str, int]:
    """The outcome tallies an earlier attempt already committed."""
    previous = previous if isinstance(previous, dict) and previous.get("stage") == _STAGE else {}
    return {name: _count(previous, name) for name in PUBLICATION_OUTCOMES}


def record_publication_applied(progress: dict) -> dict:
    return {**progress, "applied": _count(progress, "applied") + 1}


def record_publication_skip(progress: dict) -> dict:
    attempt_skipped = _count(progress, "attempt_skipped") + 1
    # An inactive suggestion remains approved and can be skipped again on retry.
    # `max` prevents recounting that same item; it may temporarily under-count a
    # mixture of old and new skips, which success/terminal reconciliation repairs.
    return {
        **progress,
        "skipped": max(_count(progress, "skipped"), attempt_skipped),
        "attempt_skipped": attempt_skipped,
    }


def record_publication_failure(progress: dict) -> dict:
    attempt_failed = _count(progress, "attempt_failed") + 1
    return {
        **progress,
        "failed": attempt_failed,
        "attempt_failed": attempt_failed,
        "attempt_failures": _count(progress, "attempt_failures") + 1,
    }


def complete_publication_success(progress: dict) -> dict:
    applied = _count(progress, "applied")
    total = max(_count(progress, "total"), applied)
    return {
        **progress,
        "failed": 0,
        "skipped": max(total - applied, 0),
        "total": total,
        "attempt_failed": 0,
        "failure_state": None,
    }


def mark_publication_failure(progress: dict | None, *, terminal: bool) -> dict | None:
    if not isinstance(progress, dict) or progress.get("stage") != _STAGE:
        return None
    updated = {
        **progress,
        "failed": _count(progress, "attempt_failed", _count(progress, "failed")),
        "failure_state": "terminal" if terminal else "retrying",
    }
    if terminal:
        total = _count(updated, "total")
        applied = _count(updated, "applied")
        skipped = _count(updated, "skipped")
        attempt_failed = _count(updated, "attempt_failed")
        # A killed work horse may leave entries unprocessed, so terminal failures
        # include every original item not durably applied or skipped. The latest
        # attempt count is a floor for legacy or partially recorded progress.
        updated["failed"] = max(total - applied - skipped, attempt_failed, 0)
        updated["skipped"] = max(total - applied - updated["failed"], 0)
    return updated
