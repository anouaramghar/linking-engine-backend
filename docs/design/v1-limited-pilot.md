# V1 limited pilot

Status: implementation branch only. No site is enabled by the committed defaults.

## Decision

Approve a limited, reversible pilot. Do not approve a fleet-wide rollout.

The pilot uses the strongest frozen evaluation recipe:

1. retrieve dense cosine top 100;
2. retrieve structured BM25 top 100;
3. combine the candidate lists;
4. prioritize the union with weighted reciprocal-rank fusion;
5. order the evaluated top five by BM25;
6. expose BM25 ranks 1-3 in the visible queue.

Frozen parameters:

- BM25 document: title repeated 3 times, taxonomy repeated 2 times, first 512
  content terms;
- RRF dense weight: 0.25;
- RRF lexical weight: 1.0;
- RRF rank constant: 10.

The old V1 business-rule reranker remains disabled. The zero-shot reranker,
learned ranker, structured dense input, and field-aware BM25 are not part of the
pilot.

## What the fusion does, measured

**BM25-512 alone determines the evaluated top five.** The weighted RRF decides
which candidates are considered, not their final order. The visible queue then
persists ranks 1-3; this delivery cap does not change the frozen ranking recipe.

This is a property of the design rather than an observation about one corpus. A
dense-only candidate can outrank the lexical top five only if its own BM25 score
is higher — and if it were, it would already be inside the BM25 top 100. The
fusion changes the evaluated set only when fewer than five eligible lexical
candidates exist and dense-only candidates fill the remaining slots.

Rehearsals on both evaluated corpora agree, measured against real site data in
rolled-back read-only transactions on 30 July 2026:

| corpus | articles | both retrievers | lexical-only | dense-only |
| --- | --- | --- | --- | --- |
| WordPress News (site 2469) | 1,097 | 86% | 14% | **0%** |
| Airbnb (site 1) | 9,330 | 66% | 34% | **0%** |

No top-five suggestion on either corpus came from dense retrieval alone. Do not
describe the fusion as improving the final ordering; on this evidence it does
not change the evaluated top five at all. It is retained because it is the
frozen, evaluated configuration and because it broadens the pool — which is what
lets a lexical-only candidate reach an editor.

The stored `fusion_rank` and `fusion_score` components exist so this claim stays
checkable on live rows rather than resting on the rehearsal above.

## Rollout modes

Both settings are JSON arrays of explicit site IDs and default to `[]`:

```dotenv
V1_SHADOW_SITE_IDS=[12]
V1_PILOT_SITE_IDS=[]
V1_SHADOW_MAX_SOURCES=100
V1_PILOT_MAX_SUGGESTIONS_PER_ARTICLE=3
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
reports the fallback count. A failure can only cost a source its hybrid ranking;
it never expires, rewrites, or hides an existing suggestion.

#### Eligibility

Both halves of the candidate union are filtered by one shared SQL predicate
(`_PILOT_ELIGIBILITY_SQL` in `app/ml/baseline.py`), so a lexically-retrieved
candidate cannot reach an editor through a rule that dense retrieval would have
applied. The rules are:

- the target is active, on the same site, and embedded with the current model;
- no active internal link already joins the pair;
- no non-expired suggestion decision already covers the pair;
- the content fingerprints differ;
- `lower(btrim(title))` differs;
- cosine similarity is at or below `SUGGESTION_DUPLICATE_SIMILARITY_THRESHOLD`
  (0.99 by default).

BM25 knows nothing about links, prior decisions, or vectors, so the lexical pool
is pre-filtered in memory only to avoid spending its 100 slots on candidates
already known to be ineligible; the SQL predicate above is the authority, and
the worker walks the BM25 ranking in bounded pages until 100 eligible candidates
survive or the scored corpus is exhausted. Ranks are re-derived over those
survivors, so rejected candidates cannot consume the pool and hide the next
eligible result. The near-duplicate ceiling is the rule that matters most here —
it is invisible to text ranking, so without the shared predicate a lexical-only
candidate would be the one way a duplicate page reaches the queue.

This predicate applies to the pilot path only. Standard sites keep the exact
baseline query they had before the pilot, so enabling nothing changes nothing.

#### Stored score and components

`suggestions.score` is cosine semantic similarity for every method. The
dashboard percentage, its thresholds, and the global queue order all read that
one column, so a pilot row and a baseline row at 0.82 mean the same thing.

What actually selected and ordered a pilot row goes in `score_components`,
untransformed:

```json
{
  "version": "hybrid_bm25_v1",
  "final_order": "bm25_512",
  "score_is": "cosine_semantic_similarity",
  "recipe": "structured_t3_tax2_c512",
  "bm25_score": 12.47,
  "fusion": {"name": "wrrf_d025_l100_k10", "dense_weight": 0.25,
             "lexical_weight": 1.0, "rank_constant": 10},
  "fusion_rank": 3, "fusion_score": 0.0912,
  "dense_rank": 4, "lexical_rank": 2,
  "semantic": 0.82
}
```

`dense_rank` or `lexical_rank` is null when only the other retriever proposed
the target. The raw BM25 score is reported as itself; it is deliberately not
squashed into a 0–1 range, because any such number would be read as a confidence
next to the similarity percentage and it is not one. Baseline rows store no
components — their score already is their whole explanation.

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

## Queue capacity

Generation never expires anything. Standard generation keeps
`MAX_SUGGESTIONS_PER_ARTICLE` (five by default), while the visible pilot uses
`V1_PILOT_MAX_SUGGESTIONS_PER_ARTICLE` (three). Enabling the pilot cannot retire
an editor's existing queue as a side effect: a source that already has three or
more active suggestions receives nothing new until normal review or expiration
frees capacity. Sources below the cap receive only the remaining number of
suggestions.

Clearing a queue is therefore a separate, deliberate, site-scoped action:

```bash
# report only — changes nothing
python -m scripts.expire_pending_suggestions --site-id 12 --method baseline_cosine
# apply
python -m scripts.expire_pending_suggestions --site-id 12 --method baseline_cosine --yes
```

The script has no fleet-wide form — `--site-id` is required — and it only ever
touches `pending` rows. `approved`, `applying`, `applied`, and `rejected` are
editorial history and are never expired by it.

## Rollback

1. Set the site's suggestion method back to **Standard**. If it is server-managed,
   remove its ID from `V1_PILOT_SITE_IDS`.
2. Restart the analysis worker only when the environment override changed.
3. Do not trigger another pilot analysis.
4. Inspect pending and approved `hybrid_bm25` suggestions separately.
5. Expire pending pilot rows only through the site-specific operation above.
   Do not silently cancel approved or publishing rows.

Removing the flag stops new pilot ranking immediately after worker restart.
Existing editorial decisions remain durable and require an explicit operational
choice.

### Schema rollback

Both pilot migrations refuse to downgrade while the state they carry is still
live, and they refuse before changing anything — `alembic/env.py` wraps the
chain in one transaction, so a refusal leaves the schema exactly as it was:

- `b8e5f1a3c027` refuses while any `hybrid_bm25` suggestion exists, in any
  status, and reports the blocking rows by status. Dropping `score_components`
  would leave those rows in the queue with no record of how they were chosen, in
  front of an application whose method enum does not contain `hybrid_bm25`.
- `6a7d9e2c4b10` refuses while any site is still on `experimental`, because
  dropping the column would silently forget the rollout state.

A blocked rollback is recoverable; an unreadable queue is not. Clear the pending
pilot rows with the script above, decide deliberately about reviewed pilot rows,
set enrolled sites back to Standard, then downgrade.

## Dashboard behavior

The Sites page has one **Generate suggestions** action. A visible badge identifies
the saved **Standard** or **Experimental** method, and **Suggestion method…** changes
future generation without replacing existing suggestions or editorial decisions.
When the mode-specific per-source quota has no open positions, generation is
disabled with a queue-full explanation while **Compare methods** remains
available. Read-only comparison still evaluates five candidates.

The queue requests all active suggestion methods so baseline and pilot rows are both
visible during the mixed-version pilot. No queue read, count, or bulk-review request
sends a `method` filter, so a hybrid row is listed, counted, filtered, and reviewed
exactly like a baseline one — the mixed queue is the normal case during a pilot, and
a filter that quietly excluded one method would hide rows from the editor who has to
decide about them. Cards label pilot rows as `hybrid BM25`. The displayed percentage
is labelled semantic similarity in the preview; it is not presented as calibrated
BM25 confidence.

## Known limitations

- The in-memory BM25 index is rebuilt for each pilot or shadow analysis job, and
  its memory grows linearly with the corpus — roughly 0.5 GB of resident memory
  for a 9,000-article site, alongside the embedding model in the same worker.
- Offline relevance treats existing links as the known positives.
- The stored score is semantic similarity, while BM25 determines pilot selection.
  A queue sorted by score is therefore not sorted by what chose the pilot rows;
  `score_components.bm25_score` is the only place that ordering is visible.
- The pilot path applies the near-duplicate, identical-title, and identical-
  fingerprint rules; the Standard path does not. That asymmetry is deliberate —
  it keeps the rollout reversible and changes nothing fleet-wide — but it means
  a shadow comparison is comparing two slightly different eligibility sets, not
  only two rankings.
- Hybrid rows already in the queue remain visible after the site flag is removed
  until an explicit lifecycle action handles them.
- The pilot path normally issues one extra dense query per source for lexical
  eligibility, and uses additional bounded queries only when rejected candidates
  require backfilling the lexical top 100. Per-source cost was roughly 1.5–1.9×
  the baseline path on the measured corpora.

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

## Local rank-1 validation — 30 July 2026

The one-per-source cap was validated on a fresh isolated clone of WordPress News
site `2469`. Nothing was deployed or activated outside that clone.

- active sources / suggestions created: 1,097 / 1,097;
- minimum / maximum active suggestions per source: 1 / 1;
- hybrid fallbacks: 0;
- full-site generation time: 41.310s;
- peak worker working set: 812.3 MB;
- proxied suggestion API: 50 requests, 0 errors, 44.2ms p95.

Recalculation from the six frozen site/seed reports found mean Hit@1 of 6.49% for
cosine, 10.25% for BM25-512, and 10.39% for Hybrid. Hybrid therefore improved
Hit@1 by 60.2% relative to cosine on this frozen evidence.

A deterministic Codex-assisted review of 200 generated rank-1 pairs approved
116 (58%). This is an engineering audit, not independent human editorial
feedback, and does not satisfy the human expansion gate. The sampled queue rows
were left pending.

This validation establishes the quality of rank 1 only. The later product
decision to expose ranks 1-3 increases queue coverage, but does not imply that
ranks 2 and 3 independently achieved the same 58% audit result.

## Local ranks 1-3 operational validation — 30 July 2026

The three-per-source cap was then exercised on the same disposable database
after removing its prior rank-1 queue. The developer and production databases
were not changed.

- active sources / suggestions created: 1,097 / 3,291;
- minimum / maximum active suggestions per source: 3 / 3;
- hybrid fallbacks: 0;
- generation time including model loading: 43.9s;
- proxied suggestion API: 50 requests, 0 errors, 50.1ms p95;
- every row remained pending with `final_order: "bm25_512"` and
  `score_is: "cosine_semantic_similarity"`.

This rehearsal validates capacity and API behavior at the larger queue size. It
does not replace the separate editorial-quality limitation for ranks 2 and 3.
