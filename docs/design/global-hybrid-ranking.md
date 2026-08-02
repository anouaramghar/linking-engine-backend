# Global Hybrid/BM25 suggestion ranking

Status: final implementation choice. The branch is not deployed or activated in
production.

## Product contract

LinkMesh has one suggestion-generation approach:

1. retrieve the top 100 dense cosine candidates;
2. retrieve the top 100 structured BM25 candidates;
3. combine and prioritize the union with weighted reciprocal-rank fusion;
4. order the evaluated candidates by raw BM25-512 score;
5. persist up to the top three eligible suggestions per source article.

There is no Standard/Experimental choice in the API or dashboard. Every normal
analysis job attempts this Hybrid path. Cosine remains in the system for three
specific reasons:

- dense candidate retrieval;
- the stable semantic value stored in `suggestions.score`;
- automatic per-source fallback if Hybrid initialization or ranking fails.

The fallback is a reliability mechanism, not a second selectable product mode.
Fallback rows are labelled `baseline_cosine`, while successful global rows are
labelled `hybrid_bm25`, so operators can measure failures truthfully.

## Frozen ranking recipe

- BM25 document: title repeated 3 times, taxonomy repeated 2 times, first 512
  content terms;
- RRF dense weight: 0.25;
- RRF lexical weight: 1.0;
- RRF rank constant: 10;
- active suggestions per source: 3 by default;
- near-duplicate cosine ceiling: 0.99.

The configurable delivery cap is:

```dotenv
HYBRID_MAX_SUGGESTIONS_PER_ARTICLE=3
```

It may be reduced operationally but cannot exceed three. The duplicate ceiling
applies to both dense and lexical candidates through one shared SQL eligibility
predicate.

## What “Hybrid” means here

BM25-512 determines the final ordering. Weighted RRF prioritizes the combined
candidate pool before the final BM25 order; it does not blend a second score
into the final rank.

Rehearsals on 30 July 2026 found no dense-only delivered suggestion:

| corpus | articles | both retrievers | lexical-only | dense-only |
| --- | ---: | ---: | ---: | ---: |
| WordPress News, site 2469 | 1,097 | 86% | 14% | 0% |
| Airbnb, site 1 | 9,330 | 66% | 34% | 0% |

Do not claim that fusion improves the final order on this evidence. The shipped
configuration is retained because it is the frozen evaluated recipe and keeps a
broader candidate pool available when lexical eligibility is sparse.

## Eligibility

Every delivered candidate must:

- belong to the same site and be active;
- use the configured embedding model;
- not already have an active internal link from the source;
- not repeat a non-expired source/target editorial decision;
- have a different normalized title;
- have a different content fingerprint when both fingerprints exist;
- remain below the near-duplicate cosine ceiling.

These rules apply globally to both halves of the candidate union.

## Scores and review payloads

`suggestions.score` remains cosine semantic similarity for every method. Existing
percentage thresholds therefore keep one meaning across historical and new
rows.

A `hybrid_bm25` row stores a separate `score_components` JSON object containing:

- `score_is: "cosine_semantic_similarity"`;
- raw, unscaled `bm25_score`;
- `final_order: "bm25_512"`;
- dense, lexical, and fusion ranks;
- the frozen recipe and fusion constants.

The dashboard displays BM25 as a raw score, never as a second percentage.
Historical `baseline_cosine` rows remain readable and reviewable.

## Queue lifecycle

Generation never expires, hides, or replaces an editor’s rows. Pending,
approved, and applying suggestions of any method count toward the three active
slots for their source. A full source queue produces nothing; a partially full
queue receives only its remaining capacity.

Rejected, applied, and expired rows do not consume active capacity, but a
non-expired prior decision still prevents the same source/target pair from being
recreated.

Clearing pending rows remains a separate, explicit, site-scoped operation:

```bash
# dry run
python -m scripts.expire_pending_suggestions --site-id 12 --method hybrid_bm25

# apply to that site and method only
python -m scripts.expire_pending_suggestions \
  --site-id 12 --method hybrid_bm25 --yes
```

There is no fleet-wide form and reviewed history is never expired by the script.

## Failure behavior

The ranker is built once per site analysis. If initialization fails, every
eligible source uses the cosine safety fallback. If ranking fails for one
source, only that source falls back. A failed PostgreSQL read is rolled back
before the fallback query runs.

The job result and durable progress expose:

- `ranking_mode: "hybrid"`;
- `hybrid_ranker_loaded`;
- `hybrid_sources_evaluated`;
- `hybrid_fallback_sources`;
- mean dense, lexical, and union candidate counts;
- the effective per-source cap.

Fallback never expires or hides existing queue rows.

## Database and API transition

Revision `c3d7a9f1e204` makes the global choice durable:

- every existing site is set to legacy value `experimental`;
- every new site defaults to `experimental`;
- site responses expose canonical `suggestion_method: "hybrid_bm25"`;
- the compatibility API reports the method as server-managed;
- the hidden legacy per-site update route returns HTTP 409;
- the product comparison endpoint is removed.

The legacy `suggestion_mode` column stays for one release so old frontend and API
clients can read compatible responses during a rolling deployment. Generation
does not consult the column.

The Sites dashboard shows one **Hybrid** badge and one **Generate suggestions**
action. It offers neither method switching nor online comparison.

## Deployment and rollback order

Safe rollout:

1. stop or drain analysis workers;
2. deploy the backend and run the migration;
3. restart workers and verify health;
4. deploy the frontend;
5. run one bounded analysis and inspect fallback count, latency, and queue size.

Revision `c3d7a9f1e204` is the quick behavioral rollback. Downgrading it restores
all site values and the server default to `standard` without deleting or
rewriting suggestion rows. Deploy the previous application after that downgrade.

Downgrading farther past `b8e5f1a3c027` remains blocked while any
`hybrid_bm25` row exists because the older schema cannot explain or recognize
those rows. That refusal runs before destructive schema changes.

## Measured evidence and limitation

Across six frozen site/seed reports, mean Hit@1 was:

| method | mean Hit@1 |
| --- | ---: |
| cosine | 6.49% |
| BM25-512 | 10.25% |
| Hybrid | 10.39% |

Hybrid improved Hit@1 by 60.2% relative to cosine on that frozen evidence.

A deterministic Codex-assisted review of 200 generated rank-1 pairs approved
116 (58%). This is an engineering audit, not independent editorial feedback.
Ranks 2 and 3 have not independently achieved that 58% result; exposing three
is the explicit product decision to trade some precision for more coverage.

The three-per-source operational rehearsal used an isolated database:

- 1,097 active sources;
- 3,291 created suggestions, exactly 3 per source;
- 0 Hybrid fallbacks;
- 43.9s generation including model loading;
- 50 proxied API requests, 0 errors, 50.1ms p95;
- every row pending with BM25-512 final ordering and cosine semantic score.

No developer or production database was changed by that rehearsal.

## Final local verification — 30 July 2026

- backend: 324 tests passed;
- backend lint: clean;
- Alembic: one head at `c3d7a9f1e204`, no schema drift;
- frontend: 145 tests across 19 files passed;
- frontend TypeScript and ESLint: clean;
- upgraded disposable database: 7/7 sites global, all 3,291 queue rows preserved;
- live backend and frontend proxy health: database and Redis up;
- live API: canonical `hybrid_bm25`, no comparison or mode-update route exposed.
