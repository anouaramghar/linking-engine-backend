# Slice 5A: reviewer-label evidence boundary

This slice creates the evidence boundary needed before any learned ranking
experiment. It does not fit, promote, roll back, or activate a learned model.

## Eligibility

An exported label must be an immutable `reviewed` lifecycle event with:

- `review_kind=individual`;
- `exposed=true` and a recorded `shown_at`;
- a non-empty reviewer identity;
- an internal target article; and
- the complete Slice 4 ranking snapshot: retrieval version, ranking version,
  final rank, and feature snapshot.

Bulk decisions, unseen decisions, external targets, missing reviewer identity,
and incomplete ranking snapshots remain visible in readiness diagnostics but are
not exported as benchmark labels.

## Readiness gate

`GET /api/v1/evaluation/label-readiness` reports the current state. A frozen
training/evaluation artifact requires at least 100 eligible labels on each of
three representative sites. `scripts/freeze_reviewer_labels.py` calls the same
gate and exits without writing an artifact when it is not ready.

The admin JSON and CSV endpoints are evidence inspection surfaces. They can
return eligible rows before the threshold, but the response carries
`readiness.ready=false`; that response is not permission to train or promote a
model.

## Frozen splits

The JSON artifact contains:

- a deterministic time split: review events before `cutoff_at` are train and
  events at/after it are test;
- a deterministic optional site holdout: one named site's labels are test and
  every other site's labels are train; and
- the train/test site IDs and their intersection, so site leakage is visible.

Repeated review events for one suggestion are reduced to the latest event when
constructing either split. The raw evidence export still preserves every
eligible review event.
