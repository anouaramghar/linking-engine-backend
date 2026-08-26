# August 12 Review Follow-ups — Agent Implementation Plan

> Cross-repository plan for completing the August 11 publication and dashboard work. Amir's later product decisions are authoritative where they differ from older planning documents. Preserve all unrelated working-tree changes. Do not commit, push, deploy, or publish to WordPress unless the operator explicitly asks.

## Goal

Finish the confirmed correctness, recovery, contract, documentation, and formatting follow-ups without redesigning Amir's requested workflow.

The finished change must:

1. ~~make mandatory review an explicit action for every selected exact edit~~ — superseded on 2026-08-26, see Workstream 1;
2. let an operator queue durable approved plans after a reload or on another browser;
3. validate and type the asynchronous publication-preparation result end to end;
4. keep the publication migration safely reversible after preparation jobs exist;
5. align operator copy and documentation with the implemented workflow;
6. retain every approved Amir-requested dashboard improvement and leave both repositories fully validated.

## Authoritative product decisions

Treat these as requirements, not findings to undo:

- ~~Reviewing each exact edit is mandatory before approval. There is no skip-review path.~~ **Superseded 2026-08-26:** approval is no longer gated on opening each edit. The guarantee is disclosure, not a click: every selected edit is rendered in full on the approval page before the operator decides. See Workstream 1.
- The human-readable edit review is the required surface. Raw before/after HTML remains an optional advanced inspection tool; do not force operators to read raw HTML.
- One privileged dashboard admin group may approve, revoke, promote, and demote accounts. This must be enforced by the backend, not only hidden in the UI.
- Approved non-admin users retain full application access except for those access-management mutations.
- Revocation suspends access but does not silently change admin-group membership. The separate “Remove admin” action changes that membership; restoring a revoked admin therefore restores the same membership.
- Crawl and analysis failures expose a bounded reason on the site row.
- The suggestion-detail panel is collapsible on wide layouts.
- Modal headings/descriptions must not cover scrollable form content.
- Sites and content-pool sources provide a safe open-in-new-tab control.
- Running crawls periodically refresh site counts without refreshing the full application every 1.5 seconds.
- The pre-paint theme script remains a same-origin external file so the production CSP can keep rejecting inline scripts.
- The dedicated asynchronous publication-preparation worker and its retry policy remain unchanged in this follow-up. Revisit them only through a separate operational decision.

## Repositories and safety rules

- Backend: `C:\Users\formation5\LinkMesh\linking-engine-backend`
- Frontend: `C:\Users\formation5\LinkMesh\linking-engine-frontend`
- Treat them as separate Git repositories.
- Start and finish with `git status --short --branch` in both repositories.
- The current trees contain user-owned committed and uncommitted work. Never reset, restore, clean, stash, or overwrite it.
- Use `apply_patch` for intentional edits. Do not run a broad formatter before resolving the exact file list.
- Backend destructive tests must use `linkmesh_test` at `127.0.0.1:15432`, never the development `linkmesh` database.
- Do not start a real WordPress publication. This plan needs API/unit/build evidence only.

## Current baseline to revalidate

At plan creation:

- both repositories are on `feat/dashboard-auth`;
- backend commit `c8cc672` contains publication-review scaling;
- frontend commit `f95ec87` contains the streamlined exact-edit workflow;
- additional backend and frontend working-tree changes implement Amir's access and UI feedback;
- backend full suite: 704 passed;
- frontend suite: 413 passed;
- backend Ruff lint, frontend lint, frontend production build, Docker Compose config, `git diff --check`, and the single Alembic-head check passed;
- backend `ruff format --check app tests` reported 12 files needing formatting.

Re-run the state checks before editing because this snapshot can drift.

---

## Workstream 1 — Make mandatory review explicit

> **Superseded 2026-08-26.** Amir decided that direct approval is the intended
> workflow: the per-article `opened`/reviewed gate, its unread counter, and the
> "Read the next change" action are gone on purpose, and the shipped
> `PublicationReview` enables approval for every selected plan as soon as the
> batch is prepared. What survives from this workstream is the disclosure
> requirement, which the current UI still meets: every selected edit is rendered
> in full — outcome, target, and the anchor marked inside its own sentence —
> before the operator can approve, and raw HTML stays optional. The button
> wording in step 5 also still holds. Do not reintroduce the gate from the
> sections below; they are kept as the record of the earlier decision.

### Problem

`PublicationReview` currently treats the first three auto-expanded plans as read immediately. For a batch of three or fewer, approval can therefore be available without any explicit review action. That does not faithfully implement “reviewing the edit should be mandatory.”

Raw HTML is not the answer: the required review is the human-readable link/outcome/context surface already present. The smallest honest fix is to require an explicit open action for each selected article.

### Files

- Frontend: `src/components/publish/PublicationReview.tsx`
- Frontend: `src/components/publish/FlowSteps.tsx`
- Frontend: `src/pages/PublishPage.test.tsx`

### Implementation

1. Remove the automatic-read special case:
   - start every article collapsed, or otherwise ensure no article enters the `opened`/reviewed set merely because of its index;
   - derive reviewed state only from an explicit operator action that opens that article's human-readable change;
   - keep reviewed state after the operator closes the article again;
   - unticking an unread article removes it from the approval requirement, while reticking it makes review mandatory again.
2. Keep the existing “Read the next change” action. It should open the next selected unread article, mark it reviewed, and scroll it into view.
3. Do not require opening “View exact HTML (advanced).” That remains optional.
4. Change stale optional wording:
   - `FlowSteps` must no longer say “Review exact edits (recommended)”;
   - use “Review exact edits” or “Review exact edits (required).”
5. Align the final decision button with the immutable-artifact language:
   - singular: `Approve and queue 1 exact edit`;
   - plural: `Approve and queue N exact edits`.
6. Do not display or introduce a skip-review action.

### Regression tests

Add or update tests proving:

- a fresh batch, including batches of one to three articles, does not expose an approval action before explicit review;
- clicking “Read the next change” reviews exactly one selected article at a time;
- closing a reviewed article does not make it unread;
- an excluded unread article does not block approval;
- reticking an unread article blocks approval again;
- raw HTML is not required for approval;
- required wording replaces “recommended” and the final button says “exact edit(s).”

### Acceptance criteria

- No selected article is considered reviewed from render position alone.
- Approval cannot be submitted until every selected article has been explicitly opened at least once.
- The required surface stays readable and compact; raw HTML remains optional.

---

## Workstream 2 — Recover approved plans after reload

### Problem

The database durably records approved plans, and `/publish/pending` exposes `approved_plans`, but the frontend's queue-only recovery depends on the in-memory `notQueued` state. After a reload or direct visit, a site with approved plans and no newly selected suggestions is prepared again, receives an empty preparation, and offers no queue action.

The backend already defines the recovery contract: omitting `plan_ids` queues all currently approved plans for the authorized site. Reuse it; do not add another endpoint or persist browser recovery state.

### Files

- Frontend: `src/pages/PublishPage.tsx`
- Frontend: `src/components/publish/PublicationReview.tsx` only if a small reusable recovery presentation is necessary
- Frontend: `src/pages/PublishPage.test.tsx`
- Backend: `tests/test_publication_plans.py` only if the existing site-wide recovery test does not fully cover omission of `plan_ids`

### Implementation

1. Derive two independent states from `PendingPublicationSite`:
   - `selected_suggestions > 0`: new editorial intent needs preparation and review;
   - `approved_plans > 0`: durable exact edits are already approved and need queueing only.
2. On direct site entry, start preparation only when `selected_suggestions > 0` and the site can publish.
3. When `approved_plans > 0`, show a durable recovery action independent of local `notQueued` state:
   - copy: `N exact edit(s) already approved and waiting to be queued`;
   - button: `Queue approved exact edits`;
   - call the existing queue mutation without `plan_ids`, intentionally invoking the documented site-wide recovery behavior.
4. Preserve the narrower same-session retry:
   - when this browser approved a known subset and enqueueing failed, retry with those exact stored plan IDs;
   - do not replace that exact retry with site-wide queueing.
5. Handle mixed state deliberately:
   - if a site has both approved plans and newly selected suggestions, offer the approved-plan queue action and still allow preparation of the new suggestions;
   - never imply that queueing the old approvals includes the new selections.
6. Improve the site-index call to action:
   - approved-only site: `Queue approved edits`;
   - selected work present: `Review exact edits`;
   - keep the counts visible and separate.
7. Keep existing 409 and transport-error handling. Never show success until the queue request succeeds.

### Regression tests

Add tests proving:

- an approved-only direct visit does not call preparation;
- an approved-only direct visit shows queue recovery after a fresh render/reload;
- clicking recovery calls queueing with `planIds: undefined`;
- mixed approved and selected work exposes both paths without conflating them;
- a same-session approval/enqueue failure retries the exact plan IDs;
- a queue conflict or network error retains a useful retry surface;
- successful queueing invalidates pending publication, suggestions, sites, and jobs and shows the existing queued confirmation.

Confirm the backend test proves that a body-less queue request queues all and only approved plans belonging to the authorized site.

### Acceptance criteria

- Durable approved plans are queueable from a new browser session.
- Approved-only sites never enter an empty preparation flow.
- Exact-ID retry remains exact; site-wide recovery is used only when the browser no longer knows the approved subset.

---

## Workstream 3 — Type the async preparation result

### Problem

The preparation worker hand-builds anonymous dictionaries. The frontend then casts `job.data.result` through `unknown` to `PublicationPreparation`. Contract drift can therefore pass TypeScript compilation and fail only at runtime.

Keep the generic jobs endpoint. Add one narrow publication result schema and use generics on the frontend; do not build a job-type registry or a general event framework.

### Backend files

- `app/schemas/publication.py`
- `app/tasks/publication.py`
- `tests/test_publication_plans.py`

### Frontend files

- `src/types/job.ts`
- `src/api/jobs.ts`
- `src/hooks/useJobs.ts`
- `src/hooks/usePublish.ts`
- existing job/publication hook tests, adding a focused test file only if none can cover the contract cleanly

### Backend implementation

1. Add compact Pydantic models for the asynchronous result:
   - one prepared-plan summary without heavy HTML;
   - one link summary including optional `placement_context`;
   - one `PublicationPreparationJobResult` containing `site_id`, `selected_suggestions`, plans, errors, and `has_more`.
2. Reuse existing `PublicationPreparationError`, hash/status fields, and `LinkOutcome`; do not duplicate domain enums.
3. In `_prepare_publication_plans`, construct the named result model and return `model_dump(mode="json")` so the JSON stored in `JobRun.result` has been validated before persistence.
4. Keep the jobs API's general `result: dict | None` response. Publication-specific validation belongs where that result is produced and consumed.

### Frontend implementation

1. Make `JobStatus` generic with a default preserving existing callers:

   ```ts
   interface JobStatus<TResult = Record<string, unknown>> {
     result: TResult | null;
     // existing fields unchanged
   }
   ```

2. Thread the same optional generic through `getJob<TResult>()` and `useJob<TResult>()`.
3. Call `useJob<PublicationPreparation>(jobId)` from `usePreparePublicationPlans`.
4. Remove both `as unknown as PublicationPreparation` casts.
5. Do not add a runtime-validation dependency to the frontend. The backend Pydantic construction is the runtime boundary; TypeScript generics cover consumers.

### Regression tests

- Backend: validate a worker result containing plans, links, placement context, errors, and `has_more` through the new model.
- Backend: prove malformed required fields fail model construction rather than being stored.
- Frontend: prove `usePreparePublicationPlans` delivers a succeeded typed result and reports failed jobs without casts or behavior changes.
- Existing ingestion, analysis, and publication job consumers continue compiling with the default generic.

### Acceptance criteria

- No `as unknown as PublicationPreparation` remains.
- The worker cannot persist a malformed publication-preparation result.
- The generic jobs API remains backward compatible.

---

## Workstream 4 — Make the migration downgrade data-safe

### Problem

Migration `b4f1d2a7c903` widens `job_runs.kind` from 20 to 32 characters for `publication_preparation`, then blindly narrows it back to 20 during downgrade. Once such a row exists, PostgreSQL can reject the downgrade.

Historical job rows are operational evidence. Do not delete or relabel them merely to satisfy a schema downgrade.

### Files

- `alembic/versions/b4f1d2a7c903_scale_publication_review.py`
- a focused migration regression test, following the isolated scratch-database pattern already used by `tests/test_pilot_rollback.py`

### Implementation

1. Keep the widened `VARCHAR(32)` during downgrade. Dropping `requested_by` and the publication-pending index is sufficient for old application code; an older model that writes values of at most 20 characters is compatible with a wider database column.
2. Add a comment explaining why the width change is intentionally expand-only and why historical `publication_preparation` values must be preserved.
3. Do not delete preparation job rows.
4. Do not rewrite them as `publication`; that would corrupt their meaning.
5. Do not rename the job kind across the application in this follow-up.

### Regression test

Against a disposable PostgreSQL database derived from the isolated test setup:

1. upgrade through `b4f1d2a7c903` or current head;
2. insert a valid `job_runs` row whose kind is `publication_preparation`;
3. downgrade through `b4f1d2a7c903` to `a1c7e93f6b25`;
4. assert the downgrade succeeds and the historical row/kind remains intact;
5. upgrade to head again and assert one Alembic head.

The test must never target the development database.

### Acceptance criteria

- Upgrade → data insertion → downgrade → upgrade succeeds.
- Historical preparation-job identity is preserved.
- Older application code remains schema-compatible.

---

## Workstream 5 — Align documentation and finish quality gates

### Documentation and copy

Update:

- backend `README.md` dashboard-authentication section:
  - approved users have full dashboard access;
  - only admins may approve/revoke accounts or manage admin membership;
  - the restriction is backend-enforced;
  - bootstrap-admin recovery remains documented accurately.
- frontend flow copy as specified in Workstream 1.

Do not rewrite the architecture documents wholesale. Make the smallest edits required to remove contradictory statements.

### Preserve and verify Amir's other requested changes

Do not reimplement these if their current tests pass. Verify them and add only a missing regression test at a real boundary:

- non-admin access mutations return 403 from the backend;
- admin mutations succeed and the UI shows controls only to admins;
- crawl/analysis failure reasons are bounded by the backend and reachable by mouse, keyboard, and screen-reader users;
- the wide suggestion-detail panel collapses and reopens, while the overlay drawer behavior remains unchanged;
- modal description/header content remains outside the scrollable form body;
- site/content-pool external links use a new tab plus `rel="noreferrer"`;
- a running ingestion job invalidates site counts at the bounded 10-second cadence;
- `index.html` loads `/theme-boot.js`, nginx serves it under `script-src 'self'`, and the build includes the public asset.

### Formatting

1. Re-run `ruff format --check app tests` and capture the exact current list.
2. Format only the reported files required for the current branch/plan. Do not run an unbounded repository-wide rewrite.
3. At plan creation the reported files were:

   ```text
   app/api/deps.py
   app/api/routes/publish.py
   app/connectors/wordpress.py
   app/models/job.py
   app/models/suggestion.py
   app/services/job_service.py
   app/services/publication_plan_service.py
   app/tasks/queues.py
   tests/test_publication.py
   tests/test_publication_plans.py
   tests/test_sites.py
   tests/test_suggestion_queue.py
   ```

4. Before formatting each file, confirm it is part of the branch's intended scope. If a file is unrelated user-owned work, stop and report it instead of rewriting it.
5. Inspect the resulting diff to ensure formatting introduced no semantic edits.

---

## Recommended implementation order

Use small, reviewable boundaries. These are suggested commit boundaries only; they are not authorization to commit.

1. Frontend: mandatory-review semantics and copy tests.
2. Frontend: reload-safe approved-plan recovery and tests.
3. Backend + frontend: typed asynchronous preparation result.
4. Backend: data-safe migration downgrade and isolated migration test.
5. Backend/frontend: README, targeted formatting, and final verification.

Do not combine product behavior, migration behavior, and mechanical formatting into one commit.

## Verification commands

### Backend

From `C:\Users\formation5\LinkMesh\linking-engine-backend`:

```powershell
$env:DATABASE_URL='postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'
.\.venv\Scripts\python.exe -m pytest -q tests/test_publication_plans.py tests/test_dashboard_auth.py tests/test_dashboard_login_routes.py tests/test_sites.py tests/test_telegram_bot.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
.\.venv\Scripts\python.exe -m alembic heads
docker compose config --quiet
git diff --check
git status --short --branch
```

Run the isolated migration round trip described in Workstream 4 in addition to the normal suite.

### Frontend

From `C:\Users\formation5\LinkMesh\linking-engine-frontend`:

```powershell
npm.cmd run test
npm.cmd run lint
npm.cmd run build
git diff --check
git status --short --branch
```

On Windows, if Vitest or Vite fails with `spawn EPERM`, rerun the same command through the permitted execution route and report the actual test/build result separately from the sandbox error.

## Definition of done

- [ ] Every selected exact edit requires an explicit review action before approval.
- [ ] No optional/recommended review wording remains in the publication flow.
- [ ] The approval button names exact edits.
- [ ] Approved plans can be queued after reload without re-preparation.
- [ ] Same-session queue retry preserves exact plan IDs.
- [ ] Approved-only and mixed approved/selected states have distinct, truthful actions.
- [ ] Publication preparation uses a validated backend result model and typed frontend job consumption.
- [ ] No publication-preparation `unknown` cast remains.
- [ ] Migration downgrade preserves long job-kind rows and passes an isolated round trip.
- [ ] README accurately describes the backend-enforced admin group.
- [ ] Amir's other requested UI/security behaviors remain intact.
- [ ] Backend full suite, Ruff lint, Ruff formatting check, Alembic head check, Docker Compose config, and diff check pass.
- [ ] Frontend full suite, lint, build, and diff check pass.
- [ ] Both repository statuses preserve unrelated work.
- [ ] Nothing was committed, pushed, deployed, or published without explicit operator direction.

## Explicit non-goals

- no new role hierarchy beyond the single admin flag;
- no change to whether revocation preserves admin membership;
- no requirement to inspect raw HTML or display the raw plan hash during normal review;
- no replacement of the publication-preparation queue or retry policy;
- no new job endpoint, state store, dependency, analytics system, or event framework;
- no real WordPress publication test;
- no broad UI redesign or refactor of the large publication/validation pages.
