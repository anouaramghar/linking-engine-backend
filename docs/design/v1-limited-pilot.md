# V1 limited pilot

Status: implementation branch only. No site is enabled by the committed defaults.

## Decision

Approve a limited, reversible pilot. Do not approve a fleet-wide rollout.

The pilot uses the strongest frozen evaluation recipe:

1. retrieve dense cosine top 100;
2. retrieve structured BM25 top 100;
3. combine the candidate lists;
4. prioritize the union with weighted reciprocal-rank fusion;
5. select the final five in BM25 order.

Frozen parameters:

- BM25 document: title repeated 3 times, taxonomy repeated 2 times, first 512
  content terms;
- RRF dense weight: 0.25;
- RRF lexical weight: 1.0;
- RRF rank constant: 10.

The old V1 business-rule reranker remains disabled. The zero-shot reranker,
learned ranker, structured dense input, and field-aware BM25 are not part of the
pilot.

## Rollout modes

Both settings are JSON arrays of explicit site IDs and default to `[]`:

```dotenv
V1_SHADOW_SITE_IDS=[12]
V1_PILOT_SITE_IDS=[]
V1_SHADOW_MAX_SOURCES=100
```

A site cannot appear in both settings; startup validation rejects overlapping
configuration.

Each site also stores a durable suggestion method:

- `standard` uses the cosine baseline;
- `experimental` uses the hybrid pilot.

New and migrated sites default to `standard`. The environment lists remain the
operator override: a site in either list is shown as server-managed and its method
cannot be changed through the API or dashboard until the override is removed.

### Baseline

Sites absent from both lists keep the existing cosine generation path and the
existing `baseline_cosine` method.

### Shadow

The worker calculates the hybrid result but persists the existing cosine result.
The analysis job result records:

- sources evaluated;
- fallback sources;
- mean dense, lexical, and union candidate counts;
- mean top-five overlap;
- exact-order agreement rate.

Shadow errors never replace or interrupt the baseline output. The comparison uses
a deterministic evenly spaced sample, bounded by `V1_SHADOW_MAX_SOURCES`, and still
runs when existing suggestions already fill the source quota. Sources outside that
sample keep the normal baseline path.

### Pilot

The worker persists the BM25-selected candidates with method `hybrid_bm25`.
The stored score remains the pair's cosine similarity so the existing percentage
and threshold controls keep one meaning. The method identifies which ranking path
selected the pair.

If index construction or one source ranking fails, the worker logs the error and
creates that source's suggestions with the current cosine path. The job result
reports the fallback count.

### Explicit comparison

`POST /suggestions/{site_id}/compare` queues the same bounded shadow calculation
without persisting either method's suggestions. It runs even when the site's active
queue is full and reports the comparison in the durable analysis job result with
`comparison_only: true`.

The normal site generation endpoint reads the site's saved method. Both endpoints
share the existing per-site analysis job guard, so generation and comparison cannot
run concurrently for the same site.

## Deployment order

1. Deploy the migration, backend, and frontend with both site lists empty. Every site
   starts on its existing or default `standard` method.
2. Start the API and worker and verify health.
3. Use **Compare methods** on one low-risk site and review the durable job result
   and latency.
4. Set that site's suggestion method to **Experimental** in the dashboard, or use
   `V1_PILOT_SITE_IDS` when the rollout must remain deployment-managed.
5. Restart the worker only when an environment override changed.
6. Trigger analysis only after confirming the site's current active suggestion
   quota has room for new candidates.
7. Add a second low-risk site only after the first site's reliability is stable.

Existing active suggestions are never silently expired. If a site's five-per-source
quota is already full, the pilot will create suggestions only as decisions or
normal expiration free capacity.

## Pilot gates

Before visible activation:

- zero unexpected analysis failures;
- automatic fallback verified;
- stable candidate counts;
- p95 analysis latency judged acceptable against the existing path;
- no change to publication or review lifecycle behavior.

Before expansion:

- at least 200 explicit editorial decisions across enabled sites;
- at least 50 decisions per site where practical;
- acceptance and publishing do not regress against each site's recent baseline;
- undo, fallback, error, and latency signals remain acceptable.

These sample gates are deliberately provisional. Record the measured baseline and
agreed numerical thresholds before enabling the second site.

## Rollback

1. Set the site's suggestion method back to **Standard**. If it is server-managed,
   remove its ID from `V1_PILOT_SITE_IDS`.
2. Restart the analysis worker only when the environment override changed.
3. Do not trigger another pilot analysis.
4. Inspect pending and approved `hybrid_bm25` suggestions separately.
5. Expire pending pilot rows only through a reviewed, site-specific operation.
   Do not silently cancel approved or publishing rows.

Removing the flag stops new pilot ranking immediately after worker restart.
Existing editorial decisions remain durable and require an explicit operational
choice.

## Dashboard behavior

The Sites page has one **Generate suggestions** action. A visible badge identifies
the saved **Standard** or **Experimental** method, and **Suggestion method…** changes
future generation without replacing existing suggestions or editorial decisions.
When the five-per-source quota has no open positions, generation is disabled with a
queue-full explanation while **Compare methods** remains available.

The queue requests all active suggestion methods so baseline and pilot rows are both
visible during the mixed-version pilot. Cards label pilot rows as `hybrid BM25`. The
displayed percentage is labelled semantic similarity in the preview; it is not
presented as calibrated BM25 confidence.

## Known limitations

- The in-memory BM25 index is rebuilt for each pilot or shadow analysis job.
- Offline relevance treats existing links as the known positives.
- The stored score is semantic similarity, while BM25 determines pilot selection.
- Hybrid rows already in the queue remain visible after the site flag is removed
  until an explicit lifecycle action handles them.

## Local shadow dry run — 29 July 2026

WordPress News site `2469` was evaluated with temporary process-only shadow
configuration after the implementation tests passed. Its existing queue already
filled every source quota, so the run also exercised the full-queue path.

- shadow sources selected/evaluated: 100 / 100;
- eligible sources with free quota: 0;
- suggestions created: 0;
- hybrid fallbacks: 0;
- mean dense candidates: 99.99;
- mean lexical candidates: 100.00;
- mean union candidates: 147.69;
- mean top-five baseline/hybrid overlap: 20.8%;
- exact top-five order agreement: 0%;
- elapsed time, including model initialization and index construction: 14.015s.

The site flag was not saved. The normal worker was restored after the temporary
container exited.
