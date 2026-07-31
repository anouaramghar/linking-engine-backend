# External content pool ingestion

Content-pool sources import read-only RSS/Atom or Wikipedia content as normal
articles. A pool article can be a suggestion target for any connected site, but
a pool site can never generate or publish suggestions itself.

Create a source with `POST /api/v1/sites` and `platform: "pool"`. An RSS/Atom
feed URL selects the feed connector; a `wikipedia.org/wiki/...` URL selects the
MediaWiki connector. Pool sources default to daily crawling and remain manually
crawlable through the normal site ingestion endpoint.

Register one global repeating coordinator after deployment:

```bash
docker compose exec api python scripts/schedule_pool_ingestion.py
```

The coordinator discovers all current daily pool sources every time it runs, so
sources added later do not need their own schedule registration. It creates the
same durable ingestion jobs used by normal crawls and skips a source that already
has an active ingestion job.

The coordinator never fails on purpose. RQ schedules the next repeat only when a
job succeeds, so a coordinator that ends in `failed` would take the entire daily
chain with it and every pool source would stop refreshing until someone re-ran
the registration script. A source that cannot be queued is therefore counted in
the run's `failed` total and reported as a `pool_ingestion_enqueue_failed` alert;
a coordinator that cannot enumerate its sources at all returns an `error` in its
result and raises `pool_coordinator_failed`. Watch those two alert kinds rather
than the job state — a chain that has stopped is otherwise invisible.

Pool requests use the existing SSRF-protected transport and are bounded by
`POOL_MAX_ARTICLES_PER_SOURCE` and `POOL_SOURCE_TIMEOUT`. During suggestion
generation, missing pool embeddings are refreshed before pool articles enter the
site's Hybrid candidate corpus. If a later pool crawl deactivates a target,
pending or approved suggestions pointing to it expire during reconciliation.
