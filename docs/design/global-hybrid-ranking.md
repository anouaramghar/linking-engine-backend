# Global Hybrid standard

LinkMesh uses Hybrid retrieval as the default suggestion method. It retrieves
dense and lexical candidates, fuses the pools, and uses the frozen BM25-512
ordering for the final suggestions.

## Contract

- Every normal analysis run uses `hybrid_bm25`.
- `baseline_cosine` is reserved for explicit comparison and ranking fallback.
- Each source can have at most three active suggestions.
- A run selects at most 50 sources with open capacity.
- A site has at most 1,500 active suggestions by default; reviewed or expired
  rows free capacity for later runs.
- Reruns only fill open capacity. Generation never expires or rewrites existing
  editorial decisions.

The stored `score` remains cosine semantic similarity so the dashboard percentage
has one meaning. `score_components` records the Hybrid recipe, BM25 score, ranks,
and final ordering used to select the row.

## Existing-link correctness

Ingestion resolves WordPress internal links against the canonical article URL.
If an internal href is a redirect alias, the connector follows the redirect
before the graph is built. Exact source-target pairs, self-links, and near
duplicates remain excluded from suggestions.

## Capacity reporting

`suggestion_slots_available` is the remaining space under both limits:

```
min(active_articles * 3, HYBRID_MAX_ACTIVE_SUGGESTIONS_PER_SITE) - active_rows
```

The API and dashboard therefore stop offering generation when the review queue
is full instead of advertising the old five-per-source capacity.

## Compatibility and rollback

Migration `c3d7a9f1e204` remains in history because it may already exist in a
database. `d4e6f8a1b203` records the temporary site-scoped rollout and
`e7b4c9d2a601` restores Hybrid as the global default. Existing suggestions are
never deleted by these migrations.

Use the comparison endpoint for offline baseline checks. Do not downgrade a
database containing Hybrid rows without following the migration guard and an
explicit editorial decision about those rows.
