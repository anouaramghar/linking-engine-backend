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

## Deployment order

1. Deploy backend and frontend with both site lists empty.
2. Start the API and worker and verify health.
3. Add one low-risk site to `V1_SHADOW_SITE_IDS`.
4. Run analysis and review the durable job result and latency.
5. Remove the site from shadow, add it to `V1_PILOT_SITE_IDS`, and restart the
   worker.
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

1. Remove the site ID from `V1_PILOT_SITE_IDS`.
2. Restart the analysis worker.
3. Do not trigger another pilot analysis.
4. Inspect pending and approved `hybrid_bm25` suggestions separately.
5. Expire pending pilot rows only through a reviewed, site-specific operation.
   Do not silently cancel approved or publishing rows.

Removing the flag stops new pilot ranking immediately after worker restart.
Existing editorial decisions remain durable and require an explicit operational
choice.

## Dashboard behavior

The queue requests all active suggestion methods so baseline and pilot rows are
both visible during the mixed-version pilot. Cards label pilot rows as
`hybrid BM25`. The displayed percentage is labelled semantic similarity in the
preview; it is not presented as calibrated BM25 confidence.

## Known limitations

- The in-memory BM25 index is rebuilt for each pilot or shadow analysis job.
- Offline relevance treats existing links as the known positives.
- The stored score is semantic similarity, while BM25 determines pilot selection.
- Hybrid rows already in the queue remain visible after the site flag is removed
  until an explicit lifecycle action handles them.
