# External content-pool staging validation — 2026-08-14

This record covers an isolated local Compose staging project. It proves the
deployment configuration and real connector workflow without touching the
normal development database or queues. It is not evidence of deployment to an
external team staging host.

## Deployment

- Compose project: `linkmesh-staging`
- Host ports: API `18000`, PostgreSQL `25432`, Redis `16379`
- Alembic revision: `c3e5f7a9b201 (head)`
- API health: `status=ok`, `database=up`, `redis=up`
- One-shot services: `migrate` and `pool-scheduler-init` exited with code 0
- Daily coordinator: `scheduled` on `ingestion`, interval `86400` seconds

## Real source pilot

- Wikipedia article:
  `https://en.wikipedia.org/wiki/Internal_and_external_links`
- RSS feed:
  `https://cneos.jpl.nasa.gov/feed/news.xml`
- Both sources passed API validation, were created as `pool`, and were approved
  by the named operator identity `pilot`.
- An ingestion attempt before approval was refused with HTTP 409.
- The Wikipedia crawl stored 1 article; the RSS crawl stored 3 articles, which
  matched the pilot's configured per-source limit.
- The main JPL news feed was also probed and safely rejected after it returned
  HTTP 403. No source was created from that failed probe.

## Scheduler and ranking integration

- A manual coordinator run returned `queued=2`, `skipped=0`, `failed=0`.
- Both coordinator-created ingestion jobs completed successfully, and the
  repeating coordinator remained scheduled.
- A controlled managed-site article was seeded only in the isolated staging
  database to exercise ranking without requiring access to a customer site.
- Analysis encoded 5 articles: 1 managed article and all 4 real pool articles.
- Hybrid BM25 produced 2 pending suggestions whose targets belong to the RSS
  pool source. Their cosine scores were `0.7699` and `0.5771`.
- Tavily remained disabled: `external_searches=0` and `external_credits_used=0`.

## Result

The isolated deployment, migrations, API health, source validation, approval
guard, real RSS/Wikipedia ingestion, daily scheduling, embeddings, and pool
target suggestions all passed. Promotion to an external staging host still
requires that host's registry/image tag, secrets, DNS/TLS, and deployment
credentials.
