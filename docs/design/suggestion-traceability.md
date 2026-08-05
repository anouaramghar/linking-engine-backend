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
| Inserted | `generated` | `analysis-engine` |
| Pending to approved/rejected | `reviewed` | API actor |
| Reviewed to pending | `restored` | API actor |
| Approved to applying | `publishing` | `publication-worker` |
| Applying to applied | `applied` | `publication-worker` |
| Approved to failed | `failed` | `publication-worker` |
| Any active state to expired | `expired` | `policy-engine` |

Rows that predate the migration receive one `imported` snapshot containing
their status, method, score and stored score components. The snapshot does not
pretend the migration time was their generation time.

Operator-specific API keys record the configured operator id. The shared
service key records `service-api`, and an authentication-free development test
records `local-development`.

## API and dashboard

`GET /api/v1/suggestions/{suggestion_id}/events` returns the newest 50 events by
default and accepts a bounded `limit` up to 200. Event history is deliberately
not embedded in queue responses: the dashboard loads it only for the open
suggestion drawer, keeping queue pagination and bulk review costs independent
of history size.

The drawer explains the ranking from the data that actually selected the row:
cosine similarity for baseline suggestions and BM25 plus semantic similarity
for Hybrid suggestions. It then shows the trace id and lifecycle activity.
