# Batch ingestion-to-suggestions pipeline

The batch pipeline runs the existing ingestion and suggestion-generation steps
for several sites in one request. Each site is tracked independently, so one
failure does not stop the other sites.

## Start a batch

```http
POST /api/v1/pipelines/batches
Content-Type: application/json

{"site_ids": [12, 18, 24]}
```

The endpoint returns `202 Accepted` with the batch status and one status item
per site. Only HTML and WordPress sites are accepted. Duplicate, unknown, and
content-pool site IDs are rejected before any work is queued.

For every site, the worker runs these stages in order:

1. `ingestion`: crawl the site and store its current content.
2. `analysis`: generate internal-link suggestions from the stored content.
3. `completed`: record that both stages succeeded.

## Read progress

```http
GET /api/v1/pipelines/batches/{batch_id}
```

The response includes totals for active, succeeded, failed, and cancelled
sites. The batch ends as `succeeded`, `failed`, `partial_failed`, or
`cancelled` when no active site remains.

The dashboard keeps the active batch id in browser storage, so monitoring
survives a page refresh. It subscribes to the live event stream below and falls
back to a low-frequency status poll if the connection drops:

```http
GET /api/v1/pipelines/batches/{batch_id}/events
Accept: text/event-stream
```

The stream sends a full `batch` snapshot whenever progress changes and a final
`done` event. The UI derives an ETA from elapsed time and completed sites, and
shows the current stage, per-site result, latest error, and retry count.

## Retry one failed site

```http
POST /api/v1/pipelines/batches/{batch_id}/sites/{site_id}/retry
```

Only failed sites can be retried. The pipeline restarts the stage that failed:
an analysis failure does not crawl the site again. The retry count and latest
error are retained in the site's pipeline status.

## Cancel safely

```http
POST /api/v1/pipelines/batches/{batch_id}/cancel
```

Cancellation is scoped to this exact batch. Queued RQ jobs are removed,
started jobs receive a stop command, unfinished site runs become `cancelled`,
and completed or already-failed results are preserved. Worker stage boundaries
also re-check the durable cancellation state so a late task cannot revive the
batch after the request commits.
