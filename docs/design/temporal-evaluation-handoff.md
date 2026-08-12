# Temporal evaluation dataset and metrics handoff

## Dataset contract

Build the versioned dataset with a timezone-aware cutoff. Editorially approved
and published links are the default ground truth:

```powershell
python scripts/build_temporal_evaluation_dataset.py `
  --cutoff 2026-01-01T00:00:00+00:00 `
  --ground-truth editor `
  --output reports/evaluation/editor-v1.json
```

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
