# Agent action safety

The target is dashboard capability parity without dashboard authority parity:
the agent may understand and prepare the editor's work, but the engine decides
how much human intent each action needs before it runs.

## Action classes

| Class | Examples | Agent behavior |
| --- | --- | --- |
| Read | Search queues, inspect sites, explain suggestions, view jobs | Run immediately. |
| Reversible | Approve or reject pending suggestions, update non-secret editorial policy | Stage the exact REST request, show its effect, and require Confirm. Include an expected-state precondition and a clear reversal path. |
| Sensitive | Create managed sites, make bulk changes, start ingestion or analysis, retry or cancel a batch | Preview scope and cost, then require Confirm. Bind confirmation to the exact payload and current resource version. |
| Critical | Publish, approve publication artifacts, change credentials or roles, revoke users, permanently delete a site | Do not expose to the agent. The agent may explain the blocker and deep-link to the dashboard. |

An action is classified by its highest-risk effect. Being technically
idempotent does not make an expensive crawl safe, and being undoable does not
make a fleet-wide edit low-risk.

## Proposal contract

Agent tools remain read-only. A tool may return a proposal with:

- a closed `kind` vocabulary;
- a declared `risk` class;
- one allowlisted HTTP method and endpoint shape;
- a validated payload containing the resource's expected state or version;
- enough context for the editor to understand the exact effect.

The dashboard does not execute arbitrary proposal URLs. It switches on `kind`,
checks the method and endpoint shape, and calls the existing audited REST route.
The proposal is never authority by itself: the editor's Confirm click is.

Every confirmation is race-safe. If the resource has changed since the preview,
the REST route returns `409` and the agent must refresh instead of overwriting a
newer human or worker action.

## MCP boundary

External MCP clients can read and stage proposals, but cannot confirm them yet.
Tool annotations are useful host hints, not proof of human approval: a model can
call a second tool and repeat any token returned by the first.

When MCP execution is added, confirmation must arrive through a separate
authenticated human channel and create a short-lived, one-time action receipt
bound to the operator, tenant, action kind, canonical payload hash, and resource
version. The execution tool may consume that receipt exactly once. A receipt
must never be mintable by another MCP tool.

## Rollout

1. Individual suggestion review: reversible, exact row, expected `pending`
   status, explicit rejection reason.
2. Bulk review: already staged and undoable; add the common proposal metadata
   and keep its `match_status=pending` race guard.
3. Editorial policies: editorial ranking has a full before/after preview and
   expected-policy guard. External-link policy is sensitive: its preview names
   the exact pending and approved suggestion ids that would expire, and
   confirmation is refused if either that impact or the policy changes. Ordinary
   site metadata remains; credentials stay excluded.
4. Analysis, ingestion, retry, and cancellation now include scope, current-work
   estimates, duplicate-job checks, and a sensitive-action confirmation. Job
   starts bind to the active durable job ids. Pipeline retries bind batch/site
   status, failed stage, and retry count. Cancellation binds every unfinished
   site's status, stage, and ingestion/analysis job-run ids.
5. Managed-site creation stages normalized WordPress or HTML site records only.
   Credentials and content-pool sources are excluded. Single creation binds the
   expected absence of its URL; guarded bulk creation binds the exact sorted URL
   set and is atomic, so any duplicate or eligibility drift returns `409` rather
   than partially creating the proposal.
6. Add out-of-band MCP action receipts only after the dashboard proposal set is
   complete and audited.

Publication preparation may eventually be staged, but approving publication
plans and queuing publication remain critical human-only actions.
