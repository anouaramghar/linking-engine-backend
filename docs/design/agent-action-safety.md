# Agent action safety

The target is dashboard capability parity without dashboard authority parity:
the agent may understand and prepare the editor's work, but the engine decides
how much human intent each action needs before it runs.

## Action classes

| Class | Examples | Agent behavior |
| --- | --- | --- |
| Read | Search queues, inspect sites, explain suggestions, view jobs | Run immediately. |
| Reversible | Approve or reject pending suggestions, update non-secret editorial policy | Stage the exact REST request, show its effect, and require Confirm. Include an expected-state precondition and a clear reversal path. |
| Sensitive | Create managed sites, make bulk changes, start ingestion or analysis, schedule refreshes, retry or cancel a batch | Preview scope and cost, then require Confirm. Bind confirmation to the exact payload and current resource version. |
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

External MCP clients can read and stage proposals. A successful staging call
also returns a signed, compressed dashboard fragment containing its validated
inputs and originating principal binding. It writes no pending row and carries
no execution authority. The fragment is removed from browser history as soon as
the authenticated dashboard opens it.

The dashboard reruns the preview under that bound principal, so the editor sees
current scope and current optimistic guards. Only an approved dashboard session
can mint the opaque receipt; admin-only proposals also require the confirming
person to remain a dashboard admin. Receipt issuance binds the exact canonical
proposal hash, originating API-key/operator scope, confirming Telegram identity,
and a short expiry.

`execute_action_receipt` is the sole mutating MCP adapter and deliberately does
not belong to the shared read-only registry. It accepts no endpoint or payload.
It atomically marks the receipt consumed before dispatch, verifies the same MCP
identity and still-approved human, and then calls a closed mapping of the
existing audited route functions. Success, guarded failure, and the human actor
are persisted. A failed or interrupted attempt remains spent; the recovery is a
fresh preview and human confirmation, never replay. Publication, credentials,
roles, user revocation, and deletion have no dispatcher entry.

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
6. One-article analysis binds the exact active article and the site's active
   analysis-job snapshot. It is offered only while the article and site retain
   suggestion capacity; confirmation never broadens into site-wide analysis.
7. Alert acknowledgement binds the unread occurrence count and last-seen time,
   so a recurrence cannot be hidden by an older confirmation. Shared content-
   pool approval, revocation, and reactivation are admin-only and bind the
   exact lifecycle state; revocation additionally binds every pending or
   approved suggestion that would expire. Pool deletion stays excluded.
8. Managed-site refresh schedules use the existing durable coordinator and
   normal crawl-then-analysis pipeline. The preview binds the exact current
   schedule configuration, computes the next run in the requested timezone,
   records the dashboard actor, and refuses stale confirmations. Scheduling
   never publishes, changes credentials, or runs immediately.
9. Out-of-band MCP action receipts now cover the complete staged proposal set.
   Signed preview links remain read-only; a live dashboard session issues a
   five-minute, one-time, identity-bound receipt, and one MCP-only tool spends it
   through the closed action dispatcher.

Publication preparation may eventually be staged, but approving publication
plans and queuing publication remain critical human-only actions.
