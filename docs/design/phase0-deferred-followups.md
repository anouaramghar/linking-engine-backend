# Phase 0 Deferred Follow-ups

Recorded during the pre-push review of `feat/phase0-safety`. These are acknowledged
non-blocking follow-ups after the high-severity fixes. Each item remains open until
its acceptance checks pass.

## Medium: publication retry accounting

Publication counters currently describe one RQ attempt instead of the complete
`JobRun`. Preserve the original batch total and cumulative committed successes across
retries, persist skip/failure progress after rollback, and distinguish transient
attempt failures from terminal unresolved failures.

Acceptance: a 10-item run that applies 9 suggestions, fails 1, then applies the last
suggestion on retry finishes with `total=10` and `applied=10`; progress never resets
or regresses between attempts. All-skipped and terminal-failure runs persist accurate
final counters.

## Medium: terminal post-success status consistency

If a task commits its durable `succeeded` result and the work horse is then killed,
abandoned, or intentionally stopped before RQ records completion, the durable row
correctly preserves success while the still-live Redis job becomes `failed` or
`stopped`. Make `get_job_status()` prefer the committed durable success for this
terminal split-brain case so polling agrees with `JobRun` listings.

Acceptance: terminal killed, abandoned, and stopped callbacks after a committed
success all leave the durable result intact, and both the live job-status endpoint
and durable run APIs report `succeeded` with the same result until Redis eviction.

## Lower: platform credential invariant

Reject WordPress username/password fields when `platform="html"` instead of retaining
unused secrets. Continue requiring username and password together for WordPress sites.

Acceptance: schema and API tests return 422 without storing either secret for an HTML
site; a credential-free WordPress site remains valid.

## Lower: taxonomy collision integration coverage

Exercise category/tag ID collisions through `WordPressConnector.fetch_articles()` and
the real `_taxonomy_map()` path instead of injecting an already-correct map directly
into `_to_article()`.

Acceptance: mocked WordPress responses return category ID 7, tag ID 7, and a post that
references both; the fetched article contains both taxonomy objects with the correct
kind and name.

## Lower: job status, documentation, and retry coverage

Align `JobRun` transition comments and runtime documentation with queued retries and
the custom `LinkMeshWorker`; document the live RQ `scheduled` state; replace the
deprecated `job.exc_info` access; and add a real-Redis delayed-retry integration test.

Acceptance: documentation matches the Compose worker command, the suite has no RQ
`exc_info` deprecation warning, and a scheduled retry is rescheduled and drained by
the configured worker.

## Lower: large-crawl ingestion scaling

Replace the crawl-long live-data transaction with run-scoped staging or bounded
commits followed by a short atomic promotion/reconciliation transaction. Preserve the
current rule that an interrupted crawl cannot change the live snapshot.

Acceptance: an interrupted crawl leaves the live snapshot unchanged, while a large
synthetic crawl has bounded transaction duration and memory use and remains safely
retryable.
