# Slice 5B: reviewer-label benchmark

`run_reviewer_label_benchmark.py` compares the current BM25-512 final order with
deterministic graph controls on a frozen reviewer-label artifact.

The variants are:

- `bm25_512`: the production final-order baseline;
- `off`: graph disabled control;
- `shadow`: graph proposal beside the baseline; it must preserve baseline order;
- `active`: bounded graph reranking, measured as a comparison only.

The benchmark reports NDCG@K, MRR, approved-hit recall, explicit judged-label
precision, explicit rejected-label rate, label coverage, and paired changes
against BM25-512. Unknown candidates are not treated as rejected. Repeated
source/target judgments use the latest frozen decision and are counted in the
report as duplicate decisions.

The runner refuses to measure an artifact whose readiness gate is false, refuses
to write a zero-candidate report, and never changes `graph_reranking_mode` or
any ranking default. A benchmark result is evidence for a later decision, not a
promotion decision.

Example:

```powershell
.\.venv\Scripts\python.exe scripts/run_reviewer_label_benchmark.py `
  --split docs\data\reviewer-labels.json `
  --split-mode time `
  --k 5 `
  --output docs\data\reviewer-benchmark-<date>.json
```
