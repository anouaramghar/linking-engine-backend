# Suggestion traceability

## Contract

Every suggestion has a stable `trace_id`. Queue responses expose it so an
editor, an API log and a lifecycle event can refer to the same suggestion
without relying on an environment-specific database id.

`suggestion_events` is an append-only lifecycle stream. PostgreSQL writes the
event in the same transaction as the suggestion insert or status change, so a
state transition cannot commit without its audit entry and a rolled-back
transition cannot leave a false event behind.

## Lifecycle events

| Suggestion change | Event | Default actor |
| --- | --- | --- |
| Any suggestion inserted | `generated` | `analysis-engine` |
| Tavily candidate accepted as a suggestion (additional provenance event) | `external_discovered` | `external-search` |
| Pending to approved/rejected | `reviewed` | API actor |
| Reviewed to pending | `restored` | API actor |
| Approved to applying | `publishing` | `publication-worker` |
| Applying to applied | `applied` | `publication-worker` |
| Approved to failed | `failed` | `publication-worker` |
| Every failed remote write | `publish_attempt_failed` | `system:publication` |
| Any active state to expired | `expired` | API actor when set, otherwise `policy-engine` |
| External target invalidated by a policy change or publication preflight (additional explanation event) | `policy_expired` | API actor or `system:publication-policy` |

Every accepted Tavily candidate therefore receives both the generic `generated`
snapshot and the provider-specific `external_discovered` event.
`external_discovered` records the provider, provider request/result ids,
provider score and rank, search query, normalized URL, semantic model and score,
and the web-search safety result. The normal review and publication events then
continue on the same suggestion and `trace_id`.

`publish_attempt_failed` stores the attempt number, full failure reason, and
whether that attempt quarantined the suggestion. This keeps the complete
publication error history even though `suggestions.publish_error` intentionally
holds only the latest failure for the queue.

Rows that predate the migration receive one `imported` snapshot containing
their status, method, score and stored score components. The snapshot does not
pretend the migration time was their generation time.

Operator-specific API keys record the configured operator id. The shared
service key records `service-api`, and an authentication-free development test
records `local-development`.

## External-search decision audit

Provider results rejected before suggestion creation cannot have a
`suggestion_events` row. `external_search_audit_events` therefore records the
request outcome and every candidate decision, including candidates that never
become suggestions.

| Decision | Meaning |
| --- | --- |
| `request_completed` | Tavily returned a valid response; details include attempt count, credits, response time, result count, and provider-side exclusions |
| `request_failed` | The bounded request failed; details contain the error type and a bounded message |
| `invalid_url` | The provider URL could not be normalized safely |
| `url_too_long` | The normalized URL exceeded the storage limit |
| `safety_blocked` | HTTPS, external-links-enabled, blocklist, competitor, or owned-domain policy rejected the URL |
| `duplicate` | The canonical URL was already active for the source or repeated in the response |
| `below_threshold` | LinkMesh semantic similarity failed the global or site editorial minimum |
| `capacity_not_selected` | The candidate was eligible but all remaining suggestion slots were filled by stronger candidates |
| `accepted` | A pending direct external suggestion was created; `suggestion_id` links both audit streams |

Every row carries the site, source article, provider, query, timestamp, optional
job run and request ids, optional candidate URL/provider score, and
decision-specific JSON details. Credentials and the full provider response are
never stored. Deleting a suggestion preserves its external-search decision rows
with `suggestion_id` set to null; deleting the owning source/site removes rows
that no longer have an audit subject.

No provider request is made when the source has no open slot or title, or while
the Tavily key is absent. Those cases appear in analysis progress as
`empty_title` or `not_configured`; there is no provider request id to persist.
Analysis progress also reports `external_searches`,
`external_suggestions_created`, `external_credits_used`, and per-reason
`external_filtered` counts.

`external_search_audit_events` is currently a database-level operational audit;
it is not exposed by a public API route. The dashboard traceability page covers
accepted suggestions through `suggestion_events`, beginning with
`external_discovered`.

## API and dashboard

`GET /api/v1/suggestions/{suggestion_id}/events` returns the newest 50 events by
default and accepts a bounded `limit` up to 200. Event history is deliberately
not embedded in queue responses: the dashboard loads it only for the open
suggestion drawer, keeping queue pagination and bulk review costs independent
of history size.

The drawer explains the ranking from the data that actually selected the row:
cosine similarity for baseline and Tavily suggestions, and BM25 plus semantic
similarity for Hybrid suggestions. Tavily rows also expose `target_origin` as
`web_search`, the direct target title/URL, provider provenance, search query,
snippet, and `external_safety` checks.

The dedicated dashboard uses these endpoints:

```http
GET /api/v1/suggestion-events
GET /api/v1/suggestion-events/export.csv
```

Both accept Trace ID, actor, event type, current status, site, and date-range
filters. The event-type filter offers every event in the table above,
`external_discovered` included — it is the one event that explains a paid
external suggestion, so it cannot be the one an operator is unable to search
for. The paged dashboard exposes full event JSON, the current publication error,
and a copyable Trace ID. CSV export applies the same filters to the full matching
cohort rather than only the visible page, and streams it: the rows are read in
batches and written straight to the response, so an unfiltered export of the
largest table in the schema does not have to fit in memory first. Spreadsheet
formula prefixes are escaped before export.
