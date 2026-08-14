# External content-pool staging deployment

This runbook deploys an isolated Compose project for the RSS/Wikipedia pilot.
It does not reuse the normal development database, Redis queues, volumes, or
host ports.

## 1. Prepare configuration

```powershell
Copy-Item .env.staging.example .env.staging
```

Replace every `replace-*` value. `POSTGRES_PASSWORD` is the database bootstrap
password; its URL-encoded form belongs in `LINKMESH_DATABASE_URL`.
`CREDENTIAL_ENCRYPTION_KEY` must be a Fernet key. `OPERATOR_API_KEYS` supplies
the named human identity required for pool approval and reactivation audit
events. Never use the generic `API_KEY` for those decisions.

Keep paid providers disabled for this pilot. Set a real contact address in
`POOL_HTTP_USER_AGENT`, and allow only the reviewed RSS/Wikipedia parent
domains in `POOL_ALLOWED_DOMAINS`.

## 2. Validate and deploy

```powershell
docker compose --env-file .env.staging config --quiet
docker compose --env-file .env.staging up -d --build
docker compose --env-file .env.staging ps -a
```

The expected one-shot services are:

- `migrate`: exited with code 0 after applying Alembic migrations.
- `pool-scheduler-init`: exited with code 0 after registering the unique daily
  coordinator.

The expected long-running services include healthy PostgreSQL, Redis and API,
plus workers for ingestion, analysis, publication preparation and publication.

## 3. Verify the deployment

```powershell
Invoke-RestMethod http://127.0.0.1:18000/api/v1/health
docker compose --env-file .env.staging exec -T worker python -c "from rq.job import Job; from app.tasks.queues import redis_conn; j=Job.fetch('linkmesh-pool-daily', connection=redis_conn); print(j.id, j.get_status(refresh=True), j.origin, j.repeats_left, j.repeat_intervals)"
```

The coordinator must be `scheduled` on the `ingestion` queue with interval
`86400`. Re-running `pool-scheduler-init` is safe because the job id is unique.

## 4. Pilot a source

Use an operator-specific key in the `X-API-Key` header:

1. Validate the RSS or Wikipedia URL with
   `POST /api/v1/sites/pool-source/validate`.
2. Create it with `POST /api/v1/sites` and `platform: "pool"`.
3. Approve it with `POST /api/v1/sites/{id}/pool-source/approval`.
4. Queue a crawl with `POST /api/v1/sites/{id}/ingest`.
5. Poll `GET /api/v1/jobs/{job_id}` until it succeeds.

Verify that articles and embeddings exist, then generate suggestions for a
separate managed site. A pool article may be a target; it must never be a
source or a publication destination.

## 5. Failure checks

An unapproved source must be refused. Repeated terminal crawl failures must
increment the source counter and eventually quarantine it. Watch
`pool_ingestion_enqueue_failed` and `pool_coordinator_failed` alerts; the daily
coordinator deliberately remains successful so one source cannot stop the
repeat chain.

## 6. Promote

Before marking the deployment done, record the image tag, Alembic revision,
health result, scheduled-job result, real source URLs, crawl job ids, article
counts, and smoke-test result. Configure Telegram before exposing the dashboard
through its authenticated nginx proxy.

The repository includes the latest local isolated-pilot evidence in
[`content-pool-staging-validation-2026-08-14.md`](content-pool-staging-validation-2026-08-14.md).
