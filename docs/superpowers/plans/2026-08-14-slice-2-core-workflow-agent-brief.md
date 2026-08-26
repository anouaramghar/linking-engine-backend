# Slice 2 — Core Workflow: Agent Implementation Brief

> **Implementer:** delegated coding agent
> **Reviewer:** primary Codex agent
> **Date:** 2026-08-14
> **Scope:** LinkMesh v2 Slice 2 only

## 1. Objective

Finish and validate the existing LinkMesh suggestion-to-publication workflow so one internal-link suggestion can safely move through:

```text
Hybrid generation
→ editor selection
→ exact edit preparation
→ human-readable review
→ named approval of {id, plan_hash}
→ queueing of only those approved plans
→ drift-checked WordPress application
```

This is stabilization work, not a redesign. Much of the contract already exists in committed and uncommitted code. Inspect before editing, keep useful existing work, and change only evidence-backed gaps.

## 2. Source-of-truth order

When sources disagree, use this order:

1. This Slice 2 brief.
2. `docs/design/immutable-publication-plans.md`.
3. Current explicit product decisions in `docs/superpowers/plans/2026-08-12-review-followups.md`.
4. Current code and regression tests.
5. Older plans and historical comments.

Do not weaken a later product decision merely because older wording differs.

## 3. Starting state

Treat the following as orientation and re-check it before editing:

- Backend repository: `C:\Users\formation5\LinkMesh\linking-engine-backend`
- Frontend repository: `C:\Users\formation5\LinkMesh\linking-engine-frontend`
- Both repositories are on `feat/dashboard-auth` and ahead of their GitLab tracking branches.
- Both repositories contain substantial user-owned uncommitted work.
- Focused verification immediately before delegation passed:
  - backend: 109 tests covering publication plans, publication end-to-end behavior, Hybrid, and external-link policy;
  - frontend: 127 tests covering publication API/hooks/pages and the validation queue.
- The PBN/owned-domain protections already exist in backend policy code. Do not replace or soften them.
- External-link policies currently default to enabled; Slice 2 must make new managed sites internal-only by default without rewriting existing site decisions.

## 4. Repository safety rules

- Start and finish with `git status --short --branch` in both repositories.
- Never use reset, restore, checkout-over-files, clean, stash, mass replacement, or broad formatting.
- Never discard, overwrite, or reclassify existing uncommitted work.
- Read each file's current diff before editing it.
- Use `apply_patch` for intentional edits.
- Do not commit, push, merge, rebase, deploy, publish to WordPress, or modify Notion.
- Do not run tests against the development database. Backend destructive tests must use the isolated `linkmesh_test` database at `127.0.0.1:15432`.
- If a required edit overlaps ambiguous user-owned work and intent cannot be preserved confidently, stop and report the exact overlap instead of guessing.

## 5. Non-negotiable publication contract

The implementation must preserve all of these invariants:

1. **Selection is not publication approval.** Persisted suggestion status `approved` is presented to operators as selected editorial intent.
2. **Preparation is explicit and durable.** It may read live WordPress content and perform placement/model work, but it must write nothing to WordPress and must not be triggered implicitly by refetch, focus, remount, or ordinary query behavior.
3. **Preparation freezes the complete artifact.** The stored plan contains the exact original HTML, exact updated HTML, ordered items, schema version, and canonical hash.
4. **Review is mandatory.** Every selected plan must be explicitly opened on the human-readable exact-edit surface before its approval control becomes available. Raw HTML remains optional advanced inspection.
5. **Approval names exact artifacts.** A named operator approves exact `{id, plan_hash}` pairs atomically. A shared service key cannot perform this decision.
6. **Queueing decides nothing.** Same-session queueing names the exact approved `plan_ids`. Reload recovery may omit `plan_ids` only to queue all approved plans for the already-authorized site.
7. **The worker never reranks or rerenders.** It loads approved plans, revalidates integrity and live content, and sends the stored `updated_html` verbatim only when allowed.
8. **Drift fails closed.** Changed WordPress content produces a stale plan and no write.
9. **Queue is the only primary publication destination.** Do not restore a fleet-wide publish shortcut or a Sites-page action that bypasses exact review.
10. **Historical editorial and publication evidence remains auditable.** Do not delete or rewrite it to simplify a migration or state transition.

## 6. Implementation work

### Work package A — Reconcile and stabilize the current exact-plan work

Inspect the current backend and frontend diffs against Section 5. Do not reimplement behavior that already satisfies the contract.

Verify and fix only remaining gaps in:

- explicit exact-edit review for every selected plan;
- exact `{id, plan_hash}` approval;
- approved-but-not-queued recovery after reload;
- exact-ID retry after a same-session queue failure;
- prepared/approved/stale/failed/applied plan lifecycle;
- typed and validated async preparation results;
- queue invalidation and truthful success/error feedback;
- worker integrity, idempotency, and WordPress drift handling;
- safe Alembic upgrade/downgrade behavior for publication preparation history;
- removal of any remaining publication path that can act directly on selected suggestions.

Important boundaries:

- Do not redesign the dedicated preparation worker or retry policy.
- Do not make raw HTML mandatory for approval.
- Do not add another queue endpoint or browser persistence mechanism.
- Do not fuse selection, artifact approval, and queueing into one mutation.
- Do not treat mandatory exact-edit review as a defect.

### Work package B — Make internal-only the default for new managed sites

The v2 product scope is internal linking. Existing external-link functionality is a separate explicitly enabled track.

Implement the smallest safe transition:

1. New managed sites and newly materialized external-link policies default to `external_links_enabled = false`.
2. Creating or reading a missing policy must not silently enable external suggestions.
3. Existing persisted policy rows keep their current value. Do not bulk-disable existing sites.
4. Existing suggestions and editorial decisions must not be expired merely because the default changes.
5. An operator may still explicitly enable the separate external-link capability through the existing policy surface.
6. When disabled, the pipeline must not call Tavily, spend credits, send article titles externally, create content-pool external suggestions, or publish an external target.
7. Owned-domain, same-property, and PBN protections remain non-overridable hard blocks before ranking and before publication.
8. Backend schemas, model defaults, policy fallback behavior, API responses, frontend initial state, copy, migrations, and tests must agree on the disabled default.

Do not delete the existing external-link implementation. Do not expand or redesign it.

### Work package C — Prove the complete Slice 2 path

Add or update regression coverage for the behavior that changes. Reuse existing fixtures and patterns.

At minimum prove:

- Hybrid remains the normal internal candidate path with BM25-512 final ordering.
- A source has no more than three active suggestions.
- Selection alone cannot start publication.
- Preparation writes no WordPress content.
- Unreviewed plans cannot be approved from the UI.
- Approval sends exactly the visible `{id, plan_hash}` pairs.
- Same-session queueing sends exactly the approved plan IDs.
- Reload recovery queues authorized approved plans without re-preparation.
- No approved plan returns a conflict instead of a successful empty job.
- Hash mismatch or live-content drift performs no write.
- Already-applied exact HTML is idempotent.
- New managed sites default to external links disabled.
- A missing policy resolves to disabled.
- Existing explicit enabled/disabled policy values remain unchanged.
- Disabled external links cause no Tavily request and no external suggestion creation.
- Owned-domain/PBN rules still hard-block external candidates when the separate feature is enabled.

No real WordPress mutation is permitted. Use connector mocks or the existing controlled end-to-end test boundary.

## 7. Expected file areas

This is a guide, not permission for broad edits. Inspect current diffs first.

### Backend

- `app/models/external_policy.py`
- `app/services/external_link_policy.py`
- `app/services/external_suggestion_service.py`
- `app/api/routes/sites.py`
- `app/api/routes/publish.py`
- `app/schemas/publication.py`
- `app/services/publication_plan_service.py`
- `app/tasks/publication.py`
- relevant Alembic migration files
- focused tests under `tests/`

### Frontend

- `src/api/publish.ts`
- `src/hooks/usePublish.ts`
- `src/pages/PublishPage.tsx`
- `src/components/publish/PublicationReview.tsx`
- external-link policy creation/edit surfaces only where the backend default must be represented truthfully
- focused tests beside those files

If the correct implementation needs a clean file not listed here, explain why in the handoff. Avoid unrelated UI cleanup.

## 8. Explicit non-goals

- no graph snapshot, graph features, orphan boosting, deterministic graph reranker, or graph simulation;
- no learned reranker, training pipeline, label schema expansion, GNN, or GraphSAGE;
- no discovery/BFS expansion;
- no new WordPress plugin work;
- no broad dashboard redesign, component-system refactor, or cosmetic cleanup;
- no new analytics vendor, queue system, scheduler, state store, or runtime-validation dependency;
- no forced changes to existing external-link policy rows or editorial history;
- no guessed promotion thresholds or model changes;
- no commit, push, deployment, or live publication.

## 9. Verification commands

### Backend

Run from `C:\Users\formation5\LinkMesh\linking-engine-backend`:

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'
.\.venv\Scripts\python.exe -m pytest -q tests/test_publication_plans.py tests/test_publication_end_to_end.py tests/test_publication.py tests/test_external_link_policy.py tests/test_external_fallback.py tests/test_hybrid_pilot.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
.\.venv\Scripts\python.exe -m alembic heads
docker compose config --quiet
git diff --check
git status --short --branch
```

Use `DATABASE_URL` only where Alembic explicitly requires it, and point it to the same isolated `linkmesh_test` database. Never run a destructive migration test against development data.

### Frontend

Run from `C:\Users\formation5\LinkMesh\linking-engine-frontend`:

```powershell
npm.cmd test -- --run
npm.cmd run lint
npm.cmd run build
git diff --check
git status --short --branch
```

If Vitest/Vite/esbuild reports `spawn EPERM` in the sandbox, rerun the same command through the permitted execution route. Report the sandbox failure separately from the actual test/build result.

## 10. Definition of done

- [ ] All ten publication invariants in Section 5 are satisfied by current code and tests.
- [ ] Existing useful exact-plan work is preserved rather than replaced.
- [ ] New managed sites and missing policies default to external links disabled.
- [ ] Existing external-link policies and editorial rows are not silently changed.
- [ ] Disabled external links produce no external provider request or external suggestion.
- [ ] PBN/owned-domain guards remain explicit, non-overridable, and covered.
- [ ] Focused backend and frontend tests pass.
- [ ] Full backend tests pass against `linkmesh_test`.
- [ ] Full frontend tests, lint, and production build pass.
- [ ] Ruff, Alembic-head, Docker Compose config, and both diff checks pass, or every environmental blocker is reported precisely.
- [ ] No graph/ML/other-slice work is included.
- [ ] No unrelated user work is lost or overwritten.
- [ ] Nothing is committed, pushed, deployed, or published.

## 11. Required handoff to the reviewer

Return a concise implementation report containing:

1. exact files changed, separated by backend and frontend;
2. behavior changed and why each change was necessary;
3. behavior inspected and intentionally left unchanged because it already met the contract;
4. tests and quality commands run with exact pass/fail counts;
5. migrations added or modified and their upgrade/downgrade implications;
6. confirmation that existing policy rows/data were not rewritten;
7. remaining limitations, risks, or unverified runtime behavior;
8. final `git status --short --branch` for both repositories;
9. explicit confirmation that no commit, push, deployment, Notion edit, or WordPress publication occurred.

The reviewer will independently inspect the diff and rerun risk-proportionate validation. Passing tests alone is not approval.
