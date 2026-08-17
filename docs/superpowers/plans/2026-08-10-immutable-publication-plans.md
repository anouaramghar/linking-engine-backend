# Immutable Publication Plans Implementation Plan

> Agent-ready cross-repository plan. Work in both sibling repositories under
> `C:\Users\formation5\LinkMesh`. Do not commit or push unless the user asks after
> implementation and verification.

**Goal:** LinkMesh must publish only the exact article change a named operator saw
and approved. No model call, placement choice, target expansion, fallback choice,
or HTML rendering may happen after final approval.

**Chosen architecture:** Keep the existing suggestion decision as an internal
selection state, then introduce one immutable `PublicationPlan` per source article.
Preparation reads the live WordPress article, generates any missing placements,
renders the exact resulting HTML, and stores it. Final approval binds the operator
to a SHA-256 hash of that stored artifact. The publication worker consumes approved
plans only and sends their stored HTML only when the live source still equals the
approved original snapshot.

**Why one plan per source article:** Publication already writes one WordPress post
per source-article group. Making that same edit the approval unit keeps retries,
staleness, audit history, and partial failures independent and understandable.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL/JSONB, Alembic, RQ,
React 18, TypeScript, TanStack Query, Vitest. Use Python's `hashlib` and `json`; add
no dependency.

## Mandatory invariants

The implementation is incomplete unless every invariant below is executable in a
test:

1. A suggestion with database status `approved` is only **selected for
   preparation**. It is not a human-approved site change.
2. Final approval names a `PublicationPlan.id`, its `plan_hash`, and a human
   `approved_by` identity.
3. The hash covers the complete ordered artifact: schema version, site and source
   identity, source URL, original HTML, resulting HTML, and every link item's
   suggestion ID, target URL, anchor text, outcome, and order.
4. An approved plan's artifact fields are never updated in place. A changed plan
   is a new row with a new hash and needs a new approval.
5. Publication selects `PublicationPlan.status == "approved"`; it never selects a
   fresh cohort from `Suggestion.status == "approved"`.
6. Publication does not call OpenRouter, generate placements, invoke
   `preview_links`, re-run reciprocal arbitration, or render link HTML.
7. If live HTML equals the plan's original HTML, send exactly the stored resulting
   HTML. If it equals the resulting HTML, treat the retry as already written and
   finalize database state without another POST. For any third value, mark the
   plan stale and perform no write.
8. A suggestion attached to an approved plan cannot be undone, rejected, or added
   to another active plan.
9. Suggestions selected after a plan was prepared are not silently added to that
   plan.
10. Every UI path that can queue publication first requires an approved plan. No
    fleet-wide or Sites-page bypass remains.
11. Missing placements may resolve to a deterministic appended block, but that
    exact block must be displayed and frozen. It must never be upgraded to an
    in-text placement after approval.
12. WordPress changes between preparation and publication produce `stale`, not an
    automatic retry with newly rendered content.

## Scope and explicit non-goals

- This plan provides an application-level guarantee: LinkMesh intentionally sends
  only the approved HTML and refuses known drift.
- A WordPress compare-and-swap plugin is not included. WordPress core leaves a
  small race between the final read and POST; document this residual limitation.
- Do not rename the persisted suggestion enum value `approved` in this change.
  Relabel it as **Selected** in operator-facing copy. A database enum rename would
  add migration risk without strengthening the approval guarantee.
- Do not add plan editing, manual anchor editing, approval roles, signatures,
  retention policies, or archival storage.
- Do not backfill existing approved suggestions into approved plans. Existing
  rows remain selected and require preparation plus explicit approval.

## Current unsafe paths that must disappear

- `app/tasks/publication.py:326` generates missing placements after suggestion
  approval.
- `app/tasks/publication.py:96` recomputes the publish cohort and ordering when the
  worker starts.
- `app/connectors/wordpress.py:917` calls `preview_links` again during the write,
  so rendering is recomputed after approval.
- `app/api/routes/publish.py:55` queues all approved suggestions for a site without
  identifying an approved artifact.
- `src/pages/ValidationPage.tsx:738` publishes every pending site directly.
- `src/pages/SitesPage.tsx:727` exposes a direct **Publish approved** action.
- `src/components/suggestions/PublicationPreviewModal.tsx:65` explicitly warns
  that previewed rows may change later while still allowing publication.

## Target lifecycle

```text
Suggestion pending
    -> selected (stored internally as Suggestion.status="approved")
    -> PublicationPlan prepared
    -> PublicationPlan approved by operator + approved_hash frozen
    -> queued
    -> applied

Prepared plan + new preview          -> superseded
Approved plan + source HTML changed  -> stale; no write; suggestions return to selected
Approved plan + transient I/O error  -> remains approved for RQ retry
Approved plan + repeated hard error  -> failed; existing alert/reapproval flow remains
Worker retry + live HTML == result    -> finalize applied without another POST
```

## Persisted model

Create `app/models/publication_plan.py` with one `PublicationPlan` model and no
extra item table. Link items are immutable snapshots stored in JSONB; no current
query needs to search or update individual plan items.

Required columns:

| Column | Type | Rules |
|---|---|---|
| `id` | integer PK | Normal repository convention |
| `site_id` | FK to `sites.id` | Indexed, cascade with explicit site deletion |
| `source_article_id` | FK to `articles.id` | Indexed; one source article per plan |
| `source_url` | text | Snapshot shown to and approved by operator |
| `status` | varchar enum | `prepared`, `approved`, `applied`, `stale`, `superseded`, `failed` |
| `original_html` | text | Exact editable WordPress HTML seen during preparation |
| `updated_html` | text | Exact HTML that may be sent after approval |
| `items` | JSONB | Ordered list of exact link snapshots |
| `plan_hash` | string(64) | SHA-256 of canonical versioned artifact |
| `approved_hash` | string(64), nullable | Set equal to `plan_hash` at approval |
| `approved_by` | string(255), nullable | Value from `require_operator_identity` |
| `created_at` | timestamptz | Server default `now()` |
| `approved_at` | timestamptz, nullable | Final human approval time |
| `applied_at` | timestamptz, nullable | Successful/finalized publication time |
| `invalidated_at` | timestamptz, nullable | Stale, superseded, or failed time |
| `failure_reason` | text, nullable | Bounded diagnostic for stale/failed plans |

Add `Suggestion.publication_plan_id`, nullable and indexed, referencing
`publication_plans.id` with `ON DELETE SET NULL`. Set it only during final plan
approval, not during preparation. Keep it after success for audit traceability;
clear it when a plan becomes stale or a failed suggestion is explicitly selected
again.

Add a PostgreSQL partial unique index on `publication_plans.source_article_id`
for statuses `prepared` and `approved`. This prevents two active snapshots of the
same full WordPress post from both appearing publishable.

Use a new Alembic revision whose `down_revision` is the current head
`e8a2c4f61d90`; re-run `alembic heads` before creating it in case the working tree
has changed.

### Canonical hash input

In `app/services/publication_plan_service.py`, build this exact logical object and
serialize it using `json.dumps(..., sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` before UTF-8 encoding and SHA-256 hashing:

```json
{
  "schema_version": 1,
  "site_id": 7,
  "source_article_id": 42,
  "source_url": "https://example.com/source",
  "original_html": "<p>Before</p>",
  "updated_html": "<p>Before <a ...>anchor</a></p>",
  "items": [
    {
      "position": 0,
      "suggestion_id": 101,
      "target_url": "https://example.com/target",
      "anchor_text": "anchor",
      "outcome": "inserted"
    }
  ]
}
```

Recompute this hash from persisted fields during both approval and publication.
Never trust only the stored `plan_hash`.

## Backend interfaces

Keep the interface small and route all invariant enforcement through
`publication_plan_service.py`:

```python
prepare_site(db, site, *, max_articles: int) -> PublicationPreparation
approve_plans(db, site_id, approvals, *, approved_by: str) -> list[PublicationPlan]
load_approved_plans(db, site_id) -> list[PublicationPlan]
verify_integrity(plan) -> None
mark_stale(db, plan, reason: str) -> None
```

Do not create repository/factory classes. SQLAlchemy `Session` is already the
project's storage interface.

### HTTP contract

Replace the current dry-run contract with:

```text
POST /api/v1/publish/{site_id}/plans/prepare?max_articles=10
```

Response:

```json
{
  "site_id": 7,
  "selected_suggestions": 24,
  "plans": [
    {
      "id": 55,
      "status": "prepared",
      "plan_hash": "64 hex characters",
      "source_article_id": 42,
      "source_url": "https://example.com/source",
      "original_html": "...",
      "updated_html": "...",
      "links": []
    }
  ],
  "errors": [],
  "has_more": true
}
```

Preparation is allowed to generate and persist placements and prepared plans. It
must not write WordPress content or approve anything. An unreachable source gets
an error and no plan. `has_more` means more source articles remain unshown; it
never means they will be included in the current approval.

Final approval:

```text
POST /api/v1/publish/{site_id}/plans/approve
Cookie: dashboard session (or an operator-specific key)

{
  "plans": [
    {"id": 55, "plan_hash": "..."},
    {"id": 56, "plan_hash": "..."}
  ]
}
```

The endpoint must depend on both site authorization and
`require_operator_identity`. In one transaction, lock every named plan, require
the exact site/status/hash, recompute integrity, require every snapshotted
suggestion still has status `approved` and no `publication_plan_id`, then set
approval fields and link suggestions to their plans. Any mismatch returns 409 and
approves none.

Keep:

```text
POST /api/v1/publish/{site_id}
```

but change its meaning to queue **already-approved publication plans only**.
Return 409 when the site has no approved plans. This endpoint makes no approval
decision and can be retried safely after a queueing failure.

Change `GET /api/v1/publish/pending` to report, per site:

```json
{
  "site_id": 7,
  "selected_suggestions": 24,
  "approved_plans": 2
}
```

`selected_suggestions` counts suggestion rows awaiting preparation or re-preview;
`approved_plans` counts exact plans that may be queued/retried. Apply the existing
tenant filter to both counts.

Remove `POST /publish/{site_id}/dry-run` rather than maintaining two preparation
interfaces. Update all in-repository callers in the same coordinated change.

## Ordered implementation tasks

### Task 0: Establish a safe baseline

**Repositories:** both

- [ ] Record `git status --short --branch` separately in backend and frontend.
- [ ] Preserve every existing modification, including the untracked migration and
  frontend `NeonBorder.tsx`; do not reset, clean, stash, commit, or push.
- [ ] Confirm `alembic heads` reports only `e8a2c4f61d90`.
- [ ] Run the focused current tests before editing:

```powershell
cd C:\Users\formation5\LinkMesh\linking-engine-backend
.venv\Scripts\python.exe -m pytest -q tests/test_publication.py tests/test_placement.py tests/test_wordpress.py tests/test_suggestion_queue.py

cd C:\Users\formation5\LinkMesh\linking-engine-frontend
npm.cmd test -- --run src/api/publish.test.ts src/pages/ValidationPage.test.tsx src/pages/SitesPage.test.tsx
```

Expected: capture the actual baseline. Do not use the development database;
backend tests must use the isolated `linkmesh_test` configured by the suite.

### Task 1: Add the persisted plan model and migration

**Backend files:**

- Create `app/models/publication_plan.py`
- Modify `app/models/suggestion.py`
- Modify `app/models/__init__.py`
- Create one Alembic revision after `e8a2c4f61d90`
- Create `tests/test_publication_plans.py`

- [ ] First add tests for allowed statuses, required artifact fields, suggestion
  linkage, `ON DELETE SET NULL`, and the active-plan partial uniqueness rule.
- [ ] Add the model and migration exactly as specified above.
- [ ] Keep plan artifact fields non-null and approval metadata nullable.
- [ ] Add indexes for site/status queue reads and suggestion plan lookups.
- [ ] Upgrade a disposable/isolated database, run `alembic current`, then downgrade
  one revision and upgrade again.
- [ ] Run `alembic check` and the new model tests.

Expected: existing suggestions migrate with `publication_plan_id = NULL`; no
existing suggestion becomes publishable as a plan.

### Task 2: Build the immutable plan module and preparation flow

**Backend files:**

- Create `app/services/publication_plan_service.py`
- Modify `app/tasks/publication.py`
- Modify `app/schemas/publication.py`
- Modify `app/api/routes/publish.py`
- Extend `tests/test_publication_plans.py`
- Update relevant cases in `tests/test_publication.py`

- [ ] Add failing tests for deterministic hashing, order sensitivity, Unicode,
  artifact mutation detection, and idempotent re-hashing.
- [ ] Move `grouped_batch` and preparation-only placement orchestration out of the
  worker module into `publication_plan_service.py`. Keep placement generation
  before rendering and outside long-lived database transactions.
- [ ] For each selected source group, call the existing connector
  `preview_links` once, snapshot all exact fields, compute the hash, and persist a
  `prepared` plan.
- [ ] If an equivalent prepared plan is already active, return it. Otherwise mark
  the old prepared plan `superseded` before inserting the replacement.
- [ ] Never supersede an approved plan. Skip that source until its plan reaches a
  terminal state.
- [ ] Freeze deterministic block fallbacks in `updated_html`; remove every message
  or branch saying placement can be generated later.
- [ ] Implement the prepare route and response schema. It returns only persisted
  plans that the operator can actually approve.
- [ ] Preserve reciprocal suppression and anchor arbitration during preparation,
  and expose only the winning exact edits. No such decision remains in the worker.

Expected: preparation may call the model and WordPress GET, but performs no
WordPress POST and changes no suggestion review status.

### Task 3: Bind final approval to hash and operator identity

**Backend files:**

- Modify `app/schemas/publication.py`
- Modify `app/api/routes/publish.py`
- Modify `app/services/publication_plan_service.py`
- Modify `app/api/routes/suggestions.py`
- Extend `tests/test_publication_plans.py`
- Update `tests/test_suggestions.py` and `tests/test_suggestion_queue.py`

- [ ] Add failing tests for anonymous/shared-service-key approval rejection,
  Telegram/operator identity capture, wrong hash, mutated artifact, wrong site,
  superseded plan, duplicate approval, changed suggestion status, and partial
  batch failure.
- [ ] Implement all-or-nothing `approve_plans` with row locks and recomputed hashes.
- [ ] Set `approved_hash`, `approved_by`, `approved_at`, and suggestion
  `publication_plan_id` in the same transaction.
- [ ] Reject empty approval lists and duplicate plan IDs at schema validation.
- [ ] Change review updates so a suggestion linked to an `approved` plan is
  unreviewable. Return the existing 409 shape for individual conflicts and include
  such rows in bulk `skipped` counts. Applied suggestions remain protected by their
  existing status rule.
- [ ] A suggestion linked to a `failed` plan remains explicitly reselectable; that
  transition clears its old plan link while preserving the historical plan
  artifact. A stale plan already clears links when it becomes stale.

Expected: an operator can approve exactly the hashes returned by preparation;
changing any selected row or artifact invalidates the entire approval request.

### Task 4: Add the exact planned-edit connector operation

**Backend files:**

- Modify `app/connectors/base.py`
- Modify `app/connectors/wordpress.py`
- Modify read-only connectors only enough to keep their explicit
  `NotImplementedError` behavior
- Update `tests/test_wordpress.py`

Add one connector interface:

```python
apply_planned_edit(source, *, original_html: str, updated_html: str) -> str
```

Return only `"written"` or `"already_applied"`. Raise a typed stale-plan error
when live content is neither snapshot.

- [ ] Test current HTML equals original: one POST whose `content` is byte-for-byte
  the stored `updated_html`.
- [ ] Test current HTML equals updated: no POST and `already_applied`.
- [ ] Test current HTML differs from both: no POST and typed stale error.
- [ ] Test empty/no-op plans: no POST.
- [ ] Test that target URL, anchor, connector settings, and model changes after
  approval cannot alter the submitted HTML.
- [ ] Retain the immediate pre-write read because WordPress core has no atomic
  compare-and-swap update.
- [ ] Keep response/marker diagnostics, but do not transform approved HTML in this
  method.

Expected: `apply_planned_edit` is the only connector write interface used by the
new worker. `preview_links` remains preparation-only.

### Task 5: Make the worker consume approved plans only

**Backend files:**

- Rewrite the decision portion of `app/tasks/publication.py`
- Modify `app/services/publication_progress.py` only if an existing counter cannot
  truthfully represent plan outcomes
- Update `tests/test_publication.py`
- Update `tests/test_publication_progress.py` only when its contract changes

- [ ] Rename the task entry point to `publish_approved_plans` and update enqueue
  references. Keep the `(site_id, job_run_id=None)` shape so `enqueue_job` and
  `run_durably` need no generic refactor.
- [ ] Query approved plans at attempt start; batch size is the number of snapshotted
  link items, not all selected suggestions.
- [ ] For each plan, acquire the existing source-article advisory lock and lock the
  plan plus its linked suggestions.
- [ ] Recompute and compare `plan_hash == approved_hash` before network access.
- [ ] Treat any recomputed-hash mismatch as a non-retryable integrity failure:
  perform no network write, mark the plan and linked suggestions failed, retain
  their link for audit, and create a durable alert. Explicit reselection is the
  recovery path.
- [ ] Call only `apply_planned_edit` with stored HTML.
- [ ] On success/already-applied, set plan and linked suggestions to applied,
  copying each stored item outcome to its suggestion.
- [ ] On typed stale error, mark the plan stale, save a bounded reason, clear its
  suggestion links, leave those suggestions selected, create a durable operator
  alert, and continue with other plans. Do not ask RQ to retry a stale artifact.
- [ ] On transient connector error, roll back so the plan stays approved and let
  existing RQ retry/accounting behavior run.
- [ ] On terminal repeated failure, mark plan failed consistently with existing
  suggestion failure handling and alerts.
- [ ] Delete `generate_missing_placements` from the worker path and remove any
  worker dependency on OpenRouter/placement generation.
- [ ] Delete worker-time calls to `grouped_batch` and `connector.apply_links`.

Required regression tests:

- [ ] Worker never invokes OpenRouter, `placement_service.generate`, or
  `preview_links`.
- [ ] Suggestion selected after plan approval is untouched.
- [ ] Mutating stored artifact after approval causes no POST.
- [ ] Source drift causes no POST and makes re-preparation possible.
- [ ] Crash after remote write but before database commit finalizes on retry without
  a second POST.
- [ ] Two workers cannot publish the same plan twice.
- [ ] Existing progress, alert, retry, per-source serialization, and tenant/site
  behavior remains truthful.

### Task 6: Close backend bypasses and update operational counts

**Backend files:**

- Modify `app/api/routes/publish.py`
- Modify `app/schemas/publication.py`
- Update `tests/test_suggestion_queue.py`, `tests/test_suggestions.py`,
  `tests/test_content_pool.py`, and `tests/test_publication.py`

- [ ] Change `POST /publish/{site_id}` to return 409 without an approved plan and
  enqueue `publish_approved_plans` otherwise.
- [ ] Change `/publish/pending` and `/{site_id}/status` to distinguish selected
  suggestions from approved plans.
- [ ] Preserve tenant scoping and the content-pool publication prohibition.
- [ ] Remove the old dry-run endpoint after every frontend caller has a replacement.
- [ ] Search the whole backend for `publish_approved`, `generate_missing_placements`,
  and direct `Suggestion.status == "approved"` publication queries; only selection
  and preparation code may retain them.

Expected: a handcrafted request to the legacy queue endpoint cannot publish a
suggestion lacking a human-approved plan.

### Task 7: Replace the frontend publication contract

**Frontend files:**

- Modify `src/api/publish.ts`
- Modify `src/api/publish.test.ts`
- Modify `src/hooks/usePublish.ts`
- Stop importing `publishSite` from `src/api/sites.ts`; remove that export when no
  caller remains
- Add/update types only in `src/api/publish.ts` unless genuinely shared

- [ ] Replace `PublicationDryRun` with persisted plan response types including
  `id`, `plan_hash`, and `status`.
- [ ] Add `preparePublicationPlans(siteId)`,
  `approvePublicationPlans(siteId, [{id, plan_hash}])`, and
  `queueApprovedPlans(siteId)`.
- [ ] Model approval and queueing as two sequential mutations. If approval succeeds
  but queueing fails, retain a clear **approved, not queued** state and offer a
  queue retry without another approval.
- [ ] Invalidate suggestions, publication pending/status, sites, and jobs after the
  relevant successful mutation.
- [ ] Keep the existing 180-second preparation timeout and no automatic retry for
  live WordPress reads.

Expected: no frontend helper can publish a site directly from selected suggestion
status.

### Task 8: Make final approval explicit and remove UI bypasses

**Frontend files:**

- Modify `src/components/suggestions/PublicationPreviewModal.tsx`
- Add `src/components/suggestions/PublicationPreviewModal.test.tsx` if component
  behavior is cleaner there; otherwise extend `ValidationPage.test.tsx`
- Modify `src/components/suggestions/PublishBanner.tsx`
- Modify `src/components/suggestions/SuggestionPreview.tsx`
- Modify `src/pages/ValidationPage.tsx`
- Modify `src/pages/ValidationPage.test.tsx`
- Modify `src/pages/SitesPage.tsx`
- Modify `src/pages/SitesPage.test.tsx`
- Modify operator-facing status copy in `src/lib/utils.ts` as needed

- [ ] Relabel current suggestion decisions from **Approve/Approved** to
  **Select/Selected**. Keep request wire values unchanged.
- [ ] Change bulk-success copy from “queued for publish” to “selected for
  preparation.”
- [ ] Render each persisted plan's exact current/after HTML, links, outcomes, and
  hash-derived identity from the prepare response.
- [ ] Label the final button **Approve and queue N exact edits**.
- [ ] Send exactly the visible plan IDs and hashes. `has_more` may invite the next
  batch, but hidden plans are never approved or queued by the click.
- [ ] Errors describe sources omitted from this batch. They do not secretly join
  publication later.
- [ ] Remove the warning that placements may change later.
- [ ] Remove fleet-wide `startPublish`; when several sites have selected work,
  require choosing/previewing one site at a time.
- [ ] Replace the Sites-page **Publish approved** action with a link to
  `/queue?site=<id>&status=approved` labelled **Review publication changes**, or
  remove it if routing the filter would add more UI complexity than value.
- [ ] Provide an explicit **Queue approved edits** retry for plans already approved
  when the job was not queued.
- [ ] Keep modal focus trapping, Escape behavior, loading/error accessibility, and
  mobile layout intact.

Required frontend tests:

- [ ] Selecting a suggestion never calls a publication endpoint.
- [ ] No all-sites publication control is rendered.
- [ ] Sites page has no direct publication call.
- [ ] Final approval sends only displayed `{id, plan_hash}` pairs.
- [ ] Queueing happens only after approval resolves successfully.
- [ ] Approval success plus queue failure is shown truthfully and is retryable.
- [ ] `has_more` and source errors cannot expand the approved set.
- [ ] The modal never says content may change after approval.

### Task 9: Documentation, migration rollout, and final verification

**Backend files:**

- Modify `README.md` publication section
- Create `docs/design/immutable-publication-plans.md` as the concise durable design
  record; derive it from this plan rather than copying the task checklist
- Update any stale publication comments in config, schemas, connector, and worker

**Verification:**

- [ ] Search for stale promises and bypasses:

```powershell
rg -n "Publish approved|queued for publish|may still become|change before publication|generate_missing_placements|publish_approved\(" app tests src
```

Run from the appropriate repository or use explicit sibling paths. Expected:
zero unsafe production-code matches; test names may mention removed behavior only
when asserting its absence.

- [ ] Run backend format/lint and focused tests:

```powershell
cd C:\Users\formation5\LinkMesh\linking-engine-backend
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q tests/test_publication_plans.py tests/test_publication.py tests/test_publication_progress.py tests/test_wordpress.py tests/test_placement.py tests/test_suggestion_queue.py tests/test_suggestions.py tests/test_content_pool.py tests/test_auth.py
```

- [ ] Run the full backend suite against `linkmesh_test`, never development
  `linkmesh`:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

- [ ] Run frontend checks:

```powershell
cd C:\Users\formation5\LinkMesh\linking-engine-frontend
npm.cmd run lint
npm.cmd test -- --run
npm.cmd run build
```

- [ ] Run `git diff --check` and inspect `git status --short --branch` separately
  in both repositories.
- [ ] Build updated backend/frontend images, run the migration, and recreate only
  active application containers. Preserve PostgreSQL, Redis, and model-cache
  volumes; never use `docker compose down -v`.
- [ ] Perform a disposable end-to-end proof:
  1. Select a suggestion.
  2. Prepare a plan and record its hash.
  3. Approve it as a named operator.
  4. Change the mock/live test post before publication and prove zero write plus
     stale state.
  5. Prepare and approve a new plan.
  6. Publish and prove the submitted HTML equals the approved `updated_html`.
  7. Retry and prove no second POST.
  8. Verify the plan records `approved_by`, `approved_hash`, and `applied_at`.
- [ ] Do not push. Hand the completed changes and verification evidence back to
  the user for review.

## Deployment order

This is a coordinated contract change; use a short maintenance window unless a
separate compatibility rollout is explicitly requested.

1. Stop/drain publication workers so old code cannot publish selected rows during
   rollout.
2. Back up the database.
3. Apply the Alembic migration. Existing approved suggestions remain selected with
   null plan links.
4. Deploy backend API and publication worker together.
5. Deploy frontend immediately afterward.
6. Verify `POST /publish/{site_id}` rejects a site with selected suggestions but no
   approved plan.
7. Restart publication workers and perform one small real-site preview/approval
   only with operator authorization.

Rollback rule: rolling back application code after the migration is unsafe because
old workers ignore plans and can publish directly from selected suggestions. If
rollback is required, keep publication workers stopped until both code and schema
are restored or a forward fix is deployed.

## Definition of done

- A named operator approves hashes of exact persisted per-article changes.
- The worker publishes from approved plans only.
- No model or rendering decision occurs after approval.
- Drift produces no write and requires a new plan and approval.
- Retry after a successful remote write does not write twice.
- Existing selected backlog is not silently grandfathered into approval.
- Direct/fleet/Sites-page publication bypasses are gone.
- Backend and frontend focused/full checks pass.
- Migration upgrade/downgrade/upgrade and container smoke tests pass.
- Documentation states the remaining WordPress non-atomic read/POST limitation.
- No unrelated dirty work is lost, and nothing is committed or pushed without the
  user's explicit instruction.
