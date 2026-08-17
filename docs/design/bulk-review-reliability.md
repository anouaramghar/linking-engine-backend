# Bulk review reliability

## Explicit selection

The dashboard can select the visible page or cursor through every page to select
all suggestions matching the active filters. Explicit selections are sent in
bounded sequential chunks. If a request fails after earlier chunks commit, the
client keeps three exact sets: completed ids, ids in the failed request, and ids
not attempted yet. Editors can retry only the failed set or continue the untouched
set without replaying successful decisions.

Reject actions require confirmation. Partial success names every affected id in
the recovery panel and leaves publication-owned rows unchanged.

## Filtered rules and large undo

`POST /api/v1/suggestions/bulk-review-by-filter` applies the displayed rule in one
database statement. The same statement writes every changed suggestion into a
durable `bulk_review_operation_items` cohort. Small responses still return ids for
immediate client-side state, while large responses return an operation id without
sending a six-figure id list over the API.

```http
POST /api/v1/suggestions/bulk-review-operations/{operation_id}/undo
```

Undo restores only cohort rows that are still in the status written by that
operation. Suggestions that have since changed or entered publication are counted
as skipped and never overwritten. The endpoint records its result and is
idempotent, so a repeated request cannot undo later editorial work.
