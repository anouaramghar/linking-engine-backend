# LinkMesh production-scale readiness research and audit

Status: read-only research and code audit; 2026-08-19. No application code, database rows, migrations, or deployment state were changed.

Question: what production-scale risks matter for a multi-tenant content-linking dashboard with thousands of sites, hundreds or thousands of articles per site, background crawling/embedding/suggestion jobs, large review queues, PostgreSQL/Redis/FastAPI/RQ, and operator workflows?

Scope: both LinkMesh repositories were inspected in their current working-tree state, the local Docker/PostgreSQL runtime was queried read-only, and the external comparison uses authoritative primary documentation from PostgreSQL, Redis, FastAPI/Starlette, RQ, pgvector, HTTP/MDN, TanStack, and W3C WCAG. The local runtime image predates some current working-tree edits, so runtime observations validate the environment shape, not the latest unbuilt code. No load test was run in this audit.

## Executive answer

LinkMesh is not obviously unable to support thousands of sites, but it is not yet safe to promise that scale. The product already has several good guardrails: bounded crawl and analysis inputs, separate worker queues, cursor-based global review/publication feeds, a 100-site pipeline cap, and an explicit publication preparation limit of 10 source articles. Those choices protect the operator from the most dangerous “process everything now” workflows.

The two highest-risk paths are:

1. Large-site analysis: dense retrieval is exact pgvector search with no ANN index, while the hybrid ranker loads a whole corpus and its text into Python.
2. Large crawls: ingestion intentionally keeps a snapshot transaction open and retains article/link/discovery state in memory until the crawl is resolved and promoted.

The biggest user-experience risks are different:

- infinite-query pages are flattened and retained in the browser without a page-retention limit;
- validation groups and filters all fetched suggestions in the client even though only a bounded window is rendered;
- the publication inbox renders every loaded site row rather than virtualizing the visible window;
- fleet site listing uses offset pagination and starts with a 1,000-row page.

The practical answer is to ship a measured capacity boundary, then fix the P0 items below before onboarding a large noisy fleet. Do not solve the concern by simply raising caps: that moves pressure from the operator into PostgreSQL, worker memory, Redis, or the browser.

## Local audit: what is confirmed today

| Area | Confirmed implementation | Assessment |
| --- | --- | --- |
| Crawl safety | Customer-controlled limits include 10,000 articles, 20,000 discovered URLs, 100,000 links, 10 MB responses, and 30 minutes. | Good safety bounds; they are admission limits, not throughput proof. See [app/config.py](../../app/config.py) lines 199-217. |
| Ingestion transaction | Every accepted article is upserted during one snapshot transaction. The code explicitly trades bounded resumable commits for atomicity, and the snapshot retains article IDs, URL mappings, outbound links, and discovery observations in memory. | Confirmed high-risk path for a 10,000-article crawl. See [ingestion_service.py](../../app/services/ingestion_service.py) lines 357-452 and [crawl_snapshot.py](../../app/services/crawl_snapshot.py) lines 219-336. |
| Embedding/ranking | BGE-base 768-dimensional embeddings run on CPU by default. Analysis is capped at 10,000 articles per site and a 20,000-article corpus. Hybrid loading materializes article text and corpus structures. | Partial: bounded, but large-site memory and duration are unproven. See [config.py](../../app/config.py) lines 83-103 and 216-217, and [hybrid.py](../../app/ml/hybrid.py) lines 277-360. |
| Dense retrieval | The baseline SQL orders candidates by cosine distance and joins candidate embeddings for each source. The repository documents exact search and the live database has no HNSW or IVFFlat index. | Confirmed P0 scale risk once a site or shared corpus becomes large. See [baseline.py](../../app/ml/baseline.py) lines 57-97 and [BGE migration notes](../design/bge-base-migration.md). |
| Review API | The main suggestions endpoint uses a stable score/id cursor and a bounded page; totals are opt-in. Queue and site indexes are present. | Strong backend direction, but page size is allowed up to 1,000 and the client retains fetched pages. See [suggestions.py](../../app/api/routes/suggestions.py) lines 1081-1151 and [suggestion.py](../../app/models/suggestion.py) lines 56-95. |
| Review browser state | useSuggestions uses an infinite query and flatMaps every retained page; ValidationPage then filters and groups all fetched items, while useIncrementalList only bounds the rendered window. | Confirmed P1 UX/memory risk for long review sessions. See [useSuggestions.ts](../../../linking-engine-frontend/src/hooks/useSuggestions.ts) lines 29-45 and [ValidationPage.tsx](../../../linking-engine-frontend/src/pages/ValidationPage.tsx) lines 270-421. |
| Fleet site list | The API uses offset pagination with a 1,000-row maximum. The frontend requests a 1,000-row page and flattens all fetched pages. | Confirmed P1 risk for thousands of sites; search and summary data should be server-driven and cursor-based. See [sites.py](../../app/api/routes/sites.py) lines 417-437 and [useSites.ts](../../../linking-engine-frontend/src/hooks/useSites.ts) lines 36-44. |
| Publication inbox | The backend uses a site cursor and a 100-row maximum. Preparation is explicit and limited to 10 source articles; exact plans are stored before approval. | Strong safety and UX boundary. The loaded-site list still grows in the DOM without virtualization. See [publish.py](../../app/api/routes/publish.py) lines 126-203, [publication_plan_service.py](../../app/services/publication_plan_service.py) lines 461-535, and [PublishPage.tsx](../../../linking-engine-frontend/src/pages/PublishPage.tsx) lines 46-188. |
| Worker isolation | Ingestion, analysis, publication, and publication preparation have separate RQ queues and workers. A tenant has a 100-active-job cap and a site/kind duplicate guard. | Good isolation, but the configuration comment explicitly says it is not fair-share scheduling. Database pool sizing is left to SQLAlchemy defaults. See [queues.py](../../app/tasks/queues.py) lines 8-14, [job_service.py](../../app/services/job_service.py) lines 232-276, [config.py](../../app/config.py) lines 268-272, and [db.py](../../app/db.py) lines 1-7. |
| Pipeline progress | Pipeline batches are bounded by the frontend to 100 sites. Each stream snapshot re-reads and serializes the full batch site list, with 1-5 second backoff. | Acceptable at the current cap; do not raise the cap without aggregate/delta progress. See [pipelines.py](../../app/api/routes/pipelines.py) lines 29-63. |

### Local runtime snapshot

The healthy local stack was queried on 2026-08-19. It contained 6 sites, 314 total articles (300 active), 314 embeddings, 622 internal links, 572 suggestions, and 161 job runs. PostgreSQL total relation sizes were approximately 135 MB for articles, 50 MB for embeddings, 2.48 MB for suggestions, and 1.3 MB for internal links. The embeddings table had primary/uniqueness/article indexes but no vector ANN index.

This is useful as a sanity check, not as a capacity result. It is several orders of magnitude below the requested production shape, and the running containers were built before the current dirty working-tree edits. The necessary missing evidence is a disposable, production-shaped load test.

## Production use cases and fixes

The following scenarios are deliberately different because “thousands of sites” and “thousands of articles” stress different layers.

### 1. A fleet of 5,000 sites with 20-100 articles each

**What the operator does:** opens Sites, searches by domain/name, selects a batch, or checks summary counts.

**What can go wrong:** the first 1,000-site response is large; subsequent offset pages become more expensive at deep offsets; the client retains every loaded page and recomputes derived totals. If several operators keep the page open, invalidation and refetch multiply the same work. PostgreSQL documents that skipped OFFSET rows still have to be computed, and that a unique ORDER BY is required for deterministic pages. See [LIMIT/OFFSET](https://www.postgresql.org/docs/current/queries-limit.html).

**Fix:**

- replace fleet browsing with a small cursor page, server-side search, and server-side summary aggregates;
- return only fields needed for the row; fetch article details after entering a site;
- add a bounded client cache/page window, not an ever-growing flat list;
- keep batch actions explicitly bounded and report “selected across the current filter” separately from “loaded on this screen.”

**Priority:** P1. The current 100-site batch cap is a useful safety boundary to keep.

### 2. One site with 1,000 articles

**What the operator expects:** analysis completes in a useful time and produces a reviewable queue without freezing the API or dashboard.

**What can go wrong:** exact dense retrieval evaluates a candidate join for each source, with additional eligibility checks. Hybrid ranking also loads the corpus and text into Python. The current limits prevent an unbounded run, but they do not make the work cheap.

**Fix:**

- first measure the exact path at 100, 1,000, 5,000, and 10,000 active articles;
- add a model-aware vector retrieval strategy: HNSW or IVFFlat, a batched matrix/offline path, or a deliberately retained exact path for small sites;
- preserve exact search as the recall reference and compare recall, latency, memory, and candidate counts before switching defaults;
- treat site filtering as part of the design. pgvector documents that exact search has perfect recall while approximate search trades recall for speed, and that filtered approximate queries may need over-fetching, iterative scans, partitioning, or separate indexes. See [pgvector indexing and filtering](https://github.com/pgvector/pgvector#filtering).

**Priority:** P0. This is the most important technical capacity question.

### 3. One site with 10,000 articles and large article bodies

**What the operator expects:** ingestion and analysis are resumable, progress remains visible, and a failure does not leave a multi-hour request or an exhausted worker.

**What can go wrong:** the current snapshot keeps article IDs, URL resolution maps, outbound observations, and discovery observations in Python while live writes remain in one transaction. The hybrid ranker materializes corpus rows and text. Large text also explains why storage can grow faster than row counts; the local 314-article database already used approximately 135 MB of total article relation space.

**Fix:**

- write a run-scoped staging snapshot in chunks;
- commit durable progress and chunk data periodically, then use a short success-gated promotion step;
- persist link observations in staging rather than retaining every outbound list in the session;
- stream large reads and explicitly control ORM identity-map growth. SQLAlchemy’s yield_per is designed for very large result iteration, but must be consumed iteratively and is incompatible with some joined-eager-loading patterns. See [SQLAlchemy large result sets](https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html#fetching-large-result-sets-with-yield-per);
- keep the existing article/response/link byte limits and expose “stopped at the configured limit” as a normal job outcome.

**Priority:** P0 for any customer expected to approach the current 10,000-article ceiling.

### 4. Many small crawls plus one very large crawl

**What the operator expects:** a large customer cannot make every other customer’s dashboard and jobs feel stuck.

**What can go wrong:** separate queues prevent some starvation, but all workers still share PostgreSQL, Redis, CPU/model memory, and outbound network capacity. The current active-job cap is per tenant, not a weighted fair scheduler; a large tenant can still occupy all useful worker capacity if concurrency is increased without admission control.

**Fix:**

- define a capacity budget for database connections, model processes, Redis memory, outbound requests, and per-origin concurrency;
- add per-tenant and per-site concurrency, queue priority/reservations, and backpressure;
- schedule large analysis/crawl jobs in chunks so small jobs can interleave;
- record queue wait time separately from execution time and alert on tenant starvation;
- set explicit SQLAlchemy pool size, overflow, checkout timeout, and worker replica limits together. Never scale worker replicas independently of the database connection budget.

**Priority:** P0 before broad multi-tenant onboarding.

### 5. A review queue with 50,000-100,000 suggestions

**What the operator expects:** filters, counts, bulk decisions, and the first review cards remain responsive.

**What can go wrong:** the backend has a good cursor path, but the frontend can accumulate every fetched page, then run client-side filter/group/sort passes over the accumulated set. ValidationPage also starts several count queries for the same screen. Rendering is bounded, but computation and retained query data are not.

**Fix:**

- move source grouping, queue filtering, and “next groups” navigation behind server endpoints or use a server cursor per source group;
- lower human-facing page sizes from the engine maximum and keep only a small number of pages in the browser;
- configure a bounded infinite-query page window. TanStack documents maxPages specifically for reducing memory and sequential refetch work when many pages are loaded. See [TanStack limited infinite queries](https://tanstack.com/query/latest/docs/framework/react/guides/infinite-queries#what-if-i-want-to-limit-the-number-of-pages);
- combine related count requests into one scoped summary endpoint where possible;
- use virtualization for long rows/groups. Infinite loading alone can still leave thousands of DOM nodes; web.dev recommends virtualization plus lazy loading for large lists. See [virtualizing large lists](https://web.dev/articles/virtualize-long-lists-react-window).

**Priority:** P0/P1. P0 if large queues are a launch requirement; otherwise P1 immediately after the load test.

### 6. Thousands of sites waiting for publication review

**What the operator expects:** the inbox is searchable and calm; opening one site does not load or prepare every other site.

**What can go wrong:** the backend correctly pages the inbox and the preparation workflow is bounded, but the client renders every loaded site row and retains all loaded pages. A long “Load more” session can therefore make the DOM and React state grow.

**Fix:**

- retain the current site-cursor endpoint and explicit per-site route;
- virtualize the waiting-site index or render only a bounded window;
- add direct search/deep links and show totals without downloading all rows;
- keep preparation site-scoped and at most 10 source articles per explicit request; never add fleet-wide automatic preparation;
- show “showing N of M sites” and a clear pending/approved distinction.

**Priority:** P1. The workflow model itself is a strength and should be preserved.

### 7. A publication surge or slow customer WordPress host

**What the operator expects:** one bad host does not block every other publication and retries do not create duplicate writes or provider overload.

**What can go wrong:** publication has request delay, placement budgets, attempt limits, and durable plans, but real provider latency, rate limits, and concurrent tenants still need measurement. A retry can multiply external calls if the connector operation is not idempotent or if the outcome is unknown after a timeout.

**Fix:**

- enforce per-origin rate/concurrency limits and honor Retry-After;
- keep preparation and publication in separate queues and reserve publication capacity;
- persist attempt number, provider response category, and unknown-outcome state;
- make the write operation idempotent or reconcile by the stored plan/hash before retrying;
- test one slow host, one rate-limited host, credentials revoked mid-run, worker termination, and network timeout.

**Priority:** P0 for production publication safety; the immutable exact-plan flow is already the right foundation.

### 8. Redis restart, worker crash, or a dashboard tab left open overnight

**What the operator expects:** durable job history remains explainable and the dashboard recovers without falsely marking work successful or failed.

**What can go wrong:** Redis/RQ state, result retention, eviction, and Pub/Sub delivery are operational concerns separate from the PostgreSQL job record. TanStack Query can refetch stale queries on mount/focus/reconnect and retry failures, so multiple tabs can create request storms if polling and invalidation are not budgeted.

**Fix:**

- keep canonical job status, attempts, review decisions, and audit evidence in PostgreSQL;
- treat Redis notifications as hints and reconcile from durable status after reconnect;
- set Redis persistence, maxmemory policy, queue/result TTLs, and failure retention deliberately;
- make polling endpoint-specific, stop it for terminal jobs, and show a stale/background-refresh indicator;
- test several tabs and operators against a large queue, including reconnect and browser sleep/wake.

**Priority:** P0 operational validation, P1 for dashboard tuning.

## Prioritized implementation plan

### P0: before production-scale onboarding

1. **Build a disposable production-shaped load test.** Test at least:
   - 100 sites x 1,000 articles;
   - 1,000 sites x 100 articles;
   - one 10,000-article site;
   - 50,000 and 100,000 suggestion rows;
   - 1,000 and 5,000 waiting publication sites.

   Measure API p50/p95/p99 latency, database query plans and lock waits, worker RSS/CPU, embedding duration, queue wait, Redis memory, outbound request rate, browser heap, DOM node count, and interaction latency. Run with multiple operators/tabs and one noisy tenant. A capacity statement without these measurements is only an assumption.

2. **Resolve vector retrieval capacity.** Use exact search as the correctness reference. Prototype HNSW/IVFFlat or a batched retrieval path; benchmark per-site filtering and recall before enabling it. Do not add a global approximate index without testing tenant-filter recall.

3. **Replace large-crawl in-memory promotion with staging and resumable chunks.** Preserve the current success-gated semantics, but move article/link/discovery state into run-scoped tables or bounded chunks and make promotion explicit.

4. **Add an explicit shared-capacity budget.** Configure database pools and worker counts together; add per-tenant/site concurrency, queue backpressure, and fair scheduling. Alert on queue wait, worker memory, pool exhaustion, Redis memory, and external-origin throttling.

5. **Bound the browser data window.** Add maxPages or an equivalent eviction policy to the sites, suggestions, and publication infinite queries; replace client-side whole-queue grouping with server-driven pages; virtualize long site/group lists.

### P1: immediately after the first capacity pass

- replace site/article offset endpoints with cursor/keyset navigation where users can reach deep pages;
- return combined scoped counts and summary counters rather than four independently refetched counts for one review screen;
- keep page sizes appropriate for humans, separate from the engine’s 1,000-row safety maximum;
- add retention/archival for high-growth job, event, and diagnostic history; consider partitioning only when a measured query or retention boundary benefits from it;
- change pipeline progress snapshots to aggregate counters or deltas before raising the 100-site batch cap;
- add per-job estimated work, rate, partial-result state, and “stopped at limit” messaging.

### P2: UX polish after capacity is proven

- saved filters and deep links for site, review, and publication workspaces;
- explicit “showing N of M” and “new results arrived” states;
- pause/resume/cancel semantics that distinguish user cancellation from unknown external outcomes;
- accessible progress/status announcements, stable focus after refresh/filtering, and keyboard review through virtualized rows.

## What should not change

- Keep publication as explicit prepare, inspect, approve the exact plan/hash, then queue. Selection must not silently trigger live reads, model calls, or WordPress writes.
- Keep preparation site-scoped and bounded. A global inbox can summarize work, but it should not become a global wizard that reads thousands of live posts.
- Keep the separate worker queues and durable PostgreSQL job records.
- Keep server-side authorization and tenant/site scope on every expensive query. Client filters are a presentation aid, not a capacity or security boundary.
- Keep limits observable. A limit that silently drops work feels like data loss; a limit with progress, reason, and resume path is a product feature.

## Verification status

**Confirmed:** the static findings above are present in the current working tree; the local stack was healthy during the read-only query; the local database has no vector ANN index; publication preparation and several API/page limits are explicit.

**Partial:** the current code has sensible bounds and queue separation, but static inspection cannot establish throughput, memory headroom, fairness, recall after indexing, or browser responsiveness. The running containers do not represent every current unbuilt working-tree edit.

**Pending:** production-shaped load tests, query plans at the target cardinalities, worker/Redis failure drills, provider rate-limit behavior, multi-tab browser measurements, and product-approved latency/freshness budgets.

## Conclusion

The production boundary is bounded work. Every expensive dimension needs a limit and an observable state: rows returned, candidate vectors examined, crawl concurrency per origin and per tenant, retry attempts, database connections, Redis memory, browser DOM nodes, and operator-visible freshness. The highest-risk failure is cross-layer amplification: a large queue causes expensive SQL, the dashboard refetches it repeatedly, a worker retry multiplies external calls, and Redis or a connection pool becomes the shared bottleneck.

The design direction supported by the sources is:

- enforce tenant/site scope and deterministic ordering in the database access path, not only in route code;
- use cursor/keyset-style queue navigation and server-side filtering/counts; use `SKIP LOCKED` only for explicit work claiming;
- keep crawling, embedding, and suggestion generation out of request/in-process background work; give them separate queues, budgets, timeouts, retry policy, and durable lifecycle records;
- treat Redis persistence, eviction, TTL, and Pub/Sub delivery as separate decisions; do not make an operator audit trail depend on ephemeral Redis state;
- make the dashboard explicit about stale/placeholder data, polling, partial results, and job progress, and keep large lists server-paginated and accessible.

## Key findings

### PostgreSQL, tenancy, and queue concurrency

**C1 — Tenant isolation needs a database-level defense in depth.** PostgreSQL row-level security can constrain rows returned and modified by policy; when RLS is enabled with no applicable policy, access is default-deny. Table owners and bypass roles are important exceptions. [S1]

Implication: every article, embedding, suggestion, job, and review query should carry a tenant/site scope that is part of its data-access contract and index design. If RLS is used, test the actual runtime role, owner/bypass behavior, and both read and write policies; RLS is not a substitute for correct authorization or an excuse to omit explicit scope from query plans.

**C2 — Deep `OFFSET` pagination is an avoidable queue tax.** PostgreSQL still computes rows skipped by `OFFSET`, and subsets are not deterministic without a unique `ORDER BY`. [S2]

Implication: use a stable cursor/keyset such as `(review_priority, id)` or `(created_at, id)` and return a bounded page. Counts, filters, and sorting should be server-side; do not load a whole tenant fleet or review queue into the browser to paginate there.

**C3 — Claiming work and displaying work are different consistency problems.** `SKIP LOCKED` skips rows that cannot be locked and is explicitly described as suitable for multiple consumers of a queue-like table, but it provides an inconsistent view and is not a general-purpose read mechanism. PostgreSQL's repeatable-read and serializable modes can abort a transaction with a serialization failure; the application must retry the whole transaction. [S3][S4]

Implication: make job/article claiming a short, atomic transaction with a lease or terminal state, and make retries idempotent. Never use a queue-claim query as the source for a user-facing total or an audit report. Bulk review actions need a concurrency policy for rows that changed after the operator saw them.

**C4 — Capacity is shared even when tenants are logically separate.** `max_connections` limits concurrent database sessions and increasing it allocates more resources. Vacuum maintains reusable space, planner statistics, visibility information, and transaction-ID safety. Partitioning can help very large tables when queries prune partitions, but too many partitions increase planning and per-session memory; `EXPLAIN ANALYZE` is the measurement tool, not intuition. [S5][S6][S7][S8]

Implication: budget connections across API processes, workers, migrations, and admin tools before adding concurrency. Measure plans and wait time at representative article/suggestion volumes. Consider partitioning only around a demonstrated access or retention boundary; thousands of sites do not automatically justify one partition per site.

**C5 — Approximate vector search makes tenant filtering a recall risk.** pgvector documents exact search as perfect recall and approximate indexes as a speed/recall trade-off. HNSW uses more memory and builds more slowly than IVFFlat; with approximate indexes, filtering is applied after the index scan, so a tenant/category filter can produce fewer results. pgvector specifically notes that a shared approximate index across tenants can affect recall and speed, and recommends tenant partitioning or separate tables when isolation is required. [S9]

Implication: benchmark recall, latency, candidate counts, and memory per tenant-size band. Bound candidate generation before expensive reranking/LLM work. Treat `ef_search`, iterative scans, filter indexes/partitions, and index-build memory as workload parameters that require measured acceptance thresholds.

### Redis, RQ, crawling, and API behavior

**C6 — Redis job state is not automatically durable or permanent.** Redis documents RDB snapshots, AOF write logging, and their different loss/latency trade-offs. At `maxmemory`, the configured eviction policy may evict keys or reject writes. Pub/Sub is at-most-once: a disconnected subscriber loses the message; Redis points to Streams when persistence or stronger delivery semantics are required. RQ stores job information and results in Redis and applies TTLs to results/jobs. [S10][S11][S12][S15]

Implication: choose Redis persistence and `maxmemory-policy` for the actual role it plays. Keep canonical job status, tenant-visible history, review decisions, and audit evidence in PostgreSQL or another durable store. Use Pub/Sub as a live UI hint that can be missed, then reconcile from durable state; do not treat a notification as proof that a job completed.

**C7 — RQ concurrency and retry behavior must be designed, not assumed.** An RQ worker processes one job at a time; concurrency requires more workers. Scheduled jobs live in a scheduled registry and require a scheduler-enabled worker. Retry intervals also require the scheduler. Jobs have execution/queue/result/failure lifetimes, and forcefully killed workers can leave work abandoned or unrecorded until reconciliation. [S13][S14][S15]

Implication: separate ingestion, embedding/analysis, publication, and operator-critical work into queues or worker pools with explicit capacity. Add per-tenant admission/fairness so one large site cannot monopolize workers. Set job timeouts by workload, cap retries with backoff, persist attempts and terminal reasons, and make every retried external call safe to repeat or detectably resumable.

**C8 — Heavy crawling and model work do not belong in the web process.** FastAPI documents `BackgroundTasks` as in-process work and recommends a larger job system for heavy background computation. Synchronous FastAPI endpoints/dependencies use Starlette's thread pool; Starlette documents a default limit of 40 thread tokens shared with other synchronous work. FastAPI also notes that multiple worker processes normally do not share memory, so loading a model multiplies memory use. [S16][S17]

Implication: request handlers should validate, authorize, enqueue, and return a job resource. Keep connector deadlines, response-size limits, per-origin concurrency, cancellation, and backoff in workers. Do not increase API worker or thread counts until database connections, model memory, and external-origin limits are budgeted together.

**C9 — Async API contracts need explicit acceptance, overload, and cancellation semantics.** HTTP `202 Accepted` means accepted for processing, not completed, and the MDN example returns a monitor URL. HTTP `429` represents rate limiting and may include `Retry-After`; `Retry-After` also applies to `503`. Browser `AbortSignal.timeout()` provides a client-side deadline and distinguishes timeout from user cancellation. [S18]

Implication: enqueue endpoints should return an identifier and durable status location, never imply completion. Apply bounded retries with jitter and honor `Retry-After` for both tenant-origin crawls and dashboard calls. Make operator cancellation stop polling and, where supported, propagate cancellation to the worker without converting an unknown outcome into “failed” or “succeeded.”

### Review-queue and operator UX

**C10 — Client caching defaults can create request storms or stale decisions.** TanStack Query considers cached data stale by default, refetches stale queries on mount/focus/reconnect, and retries failed queries three times by default. Polling is independent of `staleTime`, and each observer owns a timer. Query keys must include every variable that changes the fetched data. Paginated queries can retain previous data while a new page loads, but `isPlaceholderData` identifies that the visible rows are not the new result. TanStack Table's manual pagination expects server-paginated rows plus `rowCount`/`pageCount`; TanStack's virtualization guidance says virtualization does not replace server-side pagination, filtering, or sorting. [S19][S20][S21][S22]

Implication: include tenant/site, status, filter, sort, cursor/page, and review mode in query keys. Set `staleTime`, retry rules, and polling intervals per endpoint; stop polling terminal jobs and expose a deliberate background-refresh indicator. Mark placeholder rows as updating and block actions whose selection could target stale data. Use server pagination first, then virtualize the bounded page or window; measure request rate with multiple tabs and multiple operators.

**C11 — Progress and dense controls are accessibility requirements, not polish.** WCAG 2.2 requires status messages such as waiting, progress, completion, and errors to be programmatically determinable without taking focus. It requires a keyboard-focused component not to be entirely hidden by authored content, and sets a 24 by 24 CSS-pixel minimum target size or spacing exception for pointer targets. [S23]

Implication: expose crawl/embedding/review/publish progress through an accessible status region without stealing focus; avoid a chatty announcement for every row; keep sticky trays, drawers, and dialogs from hiding focused queue controls; and give per-row/bulk controls adequate size and spacing. Test keyboard review, screen-reader announcements, focus after refresh/filtering, and recovery after a partial batch.

## Validation priorities for LinkMesh

1. Load-test tenant-scoped queue queries at large offsets and with concurrent review updates; compare cursor plans, counts, lock waits, and p95 latency.
2. Run a noisy-neighbor job test: one very large site plus many small sites, with worker, PostgreSQL connection, Redis memory, queue wait, retry, and external-origin metrics separated by tenant.
3. Exercise worker termination, Redis restart, Redis memory pressure, scheduled retries, duplicate delivery, and API timeout/cancellation. Verify that durable status and operator history remain explainable.
4. Measure vector recall and candidate counts after tenant/category filtering at several corpus sizes; record index build memory/time and query latency before enabling approximate search as a default.
5. Open the dashboard with multiple tabs/operators against a large queue; measure request rate, stale/placeholder exposure, DOM size, action race outcomes, and keyboard/screen-reader behavior.

## Claim-to-source map

| Claim | Primary evidence |
| --- | --- |
| C1 tenant isolation and RLS caveats | S1 |
| C2 deep pagination and deterministic ordering | S2 |
| C3 queue claiming and transaction retries | S3, S4 |
| C4 connections, maintenance, partitioning, and measurement | S5, S6, S7, S8 |
| C5 vector recall, filtering, multitenancy, and HNSW cost | S9 |
| C6 Redis durability, eviction, Pub/Sub, and RQ TTLs | S10, S11, S12, S15 |
| C7 worker concurrency, scheduling, retries, and failure lifecycle | S13, S14, S15 |
| C8 FastAPI/Starlette process and thread-pool limits | S16, S17 |
| C9 asynchronous HTTP, rate limiting, retry timing, and cancellation | S18 |
| C10 TanStack caching, polling, query keys, pagination, and virtualization | S19, S20, S21, S22 |
| C11 WCAG progress, focus, and target-size requirements | S23 |

## Primary-source index

- **S1 — PostgreSQL row security:** [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- **S2 — PostgreSQL pagination:** [LIMIT and OFFSET](https://www.postgresql.org/docs/current/queries-limit.html)
- **S3 — PostgreSQL work claiming:** [SELECT locking clauses](https://www.postgresql.org/docs/current/sql-select.html)
- **S4 — PostgreSQL concurrency:** [Transaction Isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
- **S5 — PostgreSQL connection capacity:** [Connections and Authentication](https://www.postgresql.org/docs/current/runtime-config-connection.html)
- **S6 — PostgreSQL maintenance:** [Routine Vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html)
- **S7 — PostgreSQL partitioning:** [Table Partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)
- **S8 — PostgreSQL measurement:** [Using EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html)
- **S9 — pgvector:** [pgvector README: indexing, filtering, multitenancy, and iterative scans](https://github.com/pgvector/pgvector#filtering)
- **S10 — Redis persistence:** [Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)
- **S11 — Redis memory pressure:** [Key eviction](https://redis.io/docs/latest/develop/reference/eviction/)
- **S12 — Redis delivery semantics:** [Pub/Sub](https://redis.io/docs/latest/develop/pubsub/) and [Streams](https://redis.io/docs/latest/develop/data-types/streams/)
- **S13 — RQ workers:** [Workers](https://python-rq.org/docs/workers/)
- **S14 — RQ scheduling and retry timing:** [Scheduling](https://python-rq.org/docs/scheduling/) and [Exceptions & Retries](https://python-rq.org/docs/exceptions/)
- **S15 — RQ job lifecycle and retention:** [Jobs](https://python-rq.org/docs/jobs/) and [Results](https://python-rq.org/docs/results/)
- **S16 — FastAPI execution model:** [Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/), [Concurrency and async/await](https://fastapi.tiangolo.com/async/), and [Deployment Concepts](https://fastapi.tiangolo.com/deployment/concepts/)
- **S17 — Starlette capacity:** [Thread Pool](https://www.starlette.io/threadpool/)
- **S18 — HTTP/API contracts:** [202 Accepted](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/202), [429 Too Many Requests](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/429), [Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After), and [AbortSignal.timeout()](https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/timeout_static)
- **S19 — TanStack Query defaults:** [Important Defaults](https://tanstack.com/query/latest/docs/framework/react/guides/important-defaults)
- **S20 — TanStack Query cache identity:** [Query Keys](https://tanstack.com/query/latest/docs/framework/react/guides/query-keys)
- **S21 — TanStack Query page/poll behavior:** [Paginated Queries](https://tanstack.com/query/latest/docs/framework/react/guides/paginated-queries) and [Polling](https://tanstack.com/query/latest/docs/framework/react/guides/polling)
- **S22 — TanStack large data rendering:** [Pagination APIs](https://tanstack.com/table/v8/docs/api/features/pagination), [Table virtualization](https://tanstack.com/table/v8/docs/guide/virtualization), and [React Virtual](https://tanstack.com/virtual/latest/docs/framework/react/react-virtual)
- **S23 — WCAG 2.2 operator interaction:** [Status Messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html), [Focus Not Obscured](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html), and [Target Size (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
