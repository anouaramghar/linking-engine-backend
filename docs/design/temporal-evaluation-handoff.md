# Temporal evaluation dataset and metrics handoff

## Dataset contract

Freeze the versioned dataset with a timezone-aware cutoff. Editorially approved
and published links are the default ground truth:

```powershell
python scripts/freeze_evaluation_split.py `
  --cutoff 2026-01-01T00:00:00+00:00 `
  --ground-truth editor
```

The file lands in `docs/data/evaluation-split-<ground-truth>-<cutoff>.json`;
`--output` overrides the path. The script reads the file back before it reports
success, so a file that cannot be loaded never survives a run.

Freeze once, then measure against the file rather than the database:

```powershell
python scripts/run_evaluation.py --split docs/data/evaluation-split-editor-2026-01-01.json
```

A split rebuilt from the database moves with the database — a crawl, an applied
suggestion or a removed site changes it — so two runs a week apart measure two
different test sets and the difference between them is not a change in ranking
quality. Every measurement of a new method, the GNN included, must name the
frozen file it used.

Use `--ground-truth observed` only as an explicit proxy when there are not
enough applied editorial suggestions. It uses the ingestion timestamp at which
an internal link was first observed and must not be described as editor-created.

Every row contains `site_id`, source and target article IDs, the event timestamp,
article creation timestamps, and flags identifying nodes created at or after the
cutoff. Events strictly before the cutoff are training examples; events at the
cutoff or later are test examples. The split is deterministic and never random.

## Colleague handoff: metrics implementation

The next task may consume `test` rows without changing the dataset builder. For
each source article, a model should produce an ordered list of candidate target
article IDs. The metrics interface should accept:

```text
correct_target_ids: set[int]
ranked_predicted_target_ids: list[int]
```

Implement Recall@K, NDCG@K, AUC, and MRR in `app/ml/evaluation/metrics.py`.
Metrics must group examples by source article, use the frozen test rows as the
only positives, and report the dataset `schema_version`, ground-truth mode, and
cutoff alongside results.

## Baseline comparison table

`run_evaluation.py` prints one row per ranking method and writes the same
summaries as JSON, keyed by method and then by site:

| method  | what it is                                                    |
| ------- | ------------------------------------------------------------- |
| lexical | BM25 over title, taxonomies and body text. Baseline 2.         |
| dense   | Cosine over the article embeddings. The V1 ranking.            |
| hybrid  | What production ships: BM25 order, fused rank breaking ties.   |

Every method sees the same source articles and the same candidate pool, so a
difference between two rows is a difference in ordering. `--method` limits the
run to one or more of them.

Read the 95% intervals before the means. Two methods whose intervals overlap
have not been separated by this test set, whatever their means say.

A new method is added in `app/ml/evaluation/ranking.py`: name it in
`RankingMethod`, add it to `RANKING_METHODS`, and produce its order inside
`EvaluationRanker.rank_all`. Nothing else in the measurement changes.

## Graph-aware multi-objective evaluation

Slice 3 keeps those relevance metrics as guardrails and adds a separate structural
report. Run it against the same frozen split used for ranking quality:

```powershell
.\.venv\Scripts\python.exe scripts\run_graph_evaluation.py `
  --split docs\data\evaluation-split-observed-2026-01-01.json `
  --k 5 `
  --output docs\data\evaluation-graph-<date>.json
```

The runner builds each site's graph from the split's training edges only, adds the
measured post-cutoff source articles, and refuses to write a report when the split
does not match the current database. For Hybrid, shadow, and active variants it
reports:

- Recall@K, NDCG@K, and MRR as relevance guardrails;
- paired reorder and relevant-hit changes against Hybrid;
- simulated orphan, underlinked, and saturation deltas;
- newly connected pages, target concentration, and simulation warnings.

Structural deltas are simulations of the top-K recommendation batch, not claims
that an editor approved or published those links. A promotion decision must state
the acceptable relevance loss and the required structural improvement explicitly;
the report does not collapse both objectives into an arbitrary single score.
