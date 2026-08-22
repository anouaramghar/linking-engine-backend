# LinkMesh suggestion quality research - 2026-08-19

This note extends `docs/research/suggestion-quality-research-2026-08-17.md`. It does not replace its
external reading. It replaces four of its code claims and its P0 model shortlist. Three things are new
here. First, the retrieval-versus-ordering question is answered with measured run telemetry instead of
being listed as experiment 1. Second, the reranker insertion point is corrected: the final-ordering seam
moved to `app/ml/candidate_ordering.py`, which the 2026-08-17 note does not mention. Third, the model
shortlist is decided against this deployment's own memory record, and the named candidate in the older
note is rejected. Where the two notes disagree, trust this one. Where this note is silent, the
2026-08-17 note still holds.

Status: research only. No application code, frontend code, database rows, migrations, or publication
behavior was changed. Every database statement in this note is a `SELECT`.

Question: how can LinkMesh improve the quality of the internal-link suggestions it produces?

## Conclusion

Build the bounded cross-encoder reranker. Build it inside `order_candidates`, not inside
`rank_hybrid_candidates`. Use `cross-encoder/ms-marco-MiniLM-L6-v2` for the first English shadow test.
Do not use `BAAI/bge-reranker-v2-m3`.

The reason is now measured, not inferred. On the four live dev sites, dense retrieval, lexical
retrieval, and their union all return the same candidate count, and that count is the whole eligible
corpus. There is no retrieval loss to recover. Every quality loss on this data is ordering loss.

The ordering decision is also large. Of 172 source articles that received suggestions, 11 received the
three targets that cosine similarity ranked first, second, and third. The other 161 received at least
one target that cosine ranked lower. The median worst delivered target sits at cosine rank 12. One sits
at cosine rank 42, in a corpus of about 44 eligible targets.

That is the finding that changes what to build. BM25-512 makes a large, systematic, unmeasured
re-ordering decision on every source article. A reranker is the correct instrument to test whether that
decision is right.

## Corrections to the 2026-08-17 note

1. **The reranker seam is named wrongly.** The older note says the BM25-first sort in
   `rank_hybrid_candidates` is "a clean seam for a reranker" (2026-08-17 note, line 34). The sort is
   still there and still BM25-first (`app/ml/hybrid.py:215-222`). It is no longer the last word on
   order. `app/ml/candidate_ordering.order_candidates` now applies the site score floor, graph
   reranking, and editorial feedback, and it builds the evidence that is persisted
   (`app/ml/candidate_ordering.py:103-195`). `generate_suggestions` calls it and writes what it returns
   (`app/services/suggestion_service.py:476-491`, `:496-516`). The older note cites neither the module
   nor the function. A reranker inserted in `hybrid.py` would be overridden downstream.

2. **Experiment 1 cannot produce a useful number on the current data.** The older note asks for
   "dense, BM25, and union Recall@100/@200 by site and source type" to separate retrieval loss from
   ordering loss (2026-08-17 note, line 132). Both retrieval pools hold 100 slots
   (`app/ml/hybrid.py:43-44`). The eligible corpus on each dev site is at most 44 targets. Both pools
   are therefore larger than the corpus, and the union is the corpus. The measurement returns the same
   number three times. See the evidence section below.

3. **The saved evaluation artifact can no longer be re-run.** The older note treats
   `docs/data/evaluation-baselines-2026-08-12.json` as historical proxy evidence, which is correct, but
   it does not record that the artifact is now unreproducible. The artifact scores site ids 2, 6, and 7.
   The dev database holds site ids 3, 9, 10, 11, and 12. The article ids in
   `docs/data/evaluation-split-observed-2026-01-01.json` are absent from the database. The artifact
   cannot serve as the before-side of any comparison.

4. **The P0 model shortlist names a model this deployment has evidence against.**
   `BAAI/bge-reranker-v2-m3` is built on `bge-m3` and has 0.6B parameters
   ([BGE reranker v2-m3 model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)). The repository's
   own `.env` records that `bge-m3` exhausts the worker memory on CPU at batch size 32 in a Docker VM of
   about 7.6 GB. The older note hedges the shortlist as "not a production recommendation", which is
   fair, but it names the one candidate the deployment already knows fails.

5. **Two citations have drifted.** The rejection-reason literal is at `app/schemas/suggestion.py:22-30`,
   not `:20-30`; line 20 is `TargetOrigin`. The lazy placement endpoint is
   `app/api/routes/suggestions.py:1232-1310`, not `:1237-1301`.

6. **The source-batch claim is right for production and wrong for shadow.** The older note says the
   generation loop orders source ids ascending. That is true when `ranking_mode` is `hybrid`
   (`app/services/suggestion_service.py:315`, `:377-380`). Shadow runs already spread their sources
   across the id range with `_evenly_spaced_ids` (`app/services/suggestion_service.py:216-222`,
   `:316-320`). The P2 rotation item applies to the production path only.

Claims in the older note that were re-checked and are still true: the Hybrid pool sizes and lexical
recipe (`app/ml/hybrid.py:43-57`, `:131-140`); the shared eligibility predicate
(`app/ml/baseline.py:112-151`, `:186-263`); the extractive and verified placement contract
(`app/services/placement_service.py:37-62`, `:114-144`, `:176-221`); the 600-character target preview
and the 12,000-character source bound (`app/services/placement_service.py:28-35`, `:156`,
`app/config.py:71-74`); graph reranking defaulting to shadow (`app/config.py:226`); the English
tokenizer and English embedding default (`app/ml/lexical.py:8-51`, `app/config.py:82-84`); and the
three-suggestion and fifty-source caps (`app/config.py:88`, `:100`).

## Evidence: the loss is ordering loss, not retrieval loss

### The measurement already exists in production telemetry

`generate_suggestions` records the mean dense pool size, the mean lexical pool size, and the mean union
size in the analysis job result (`app/services/suggestion_service.py:594-608`). This query reads them
against each site's active article count. It writes nothing.

```sql
SELECT jr.site_id,
       (jr.result->>'mean_dense_candidates')::numeric   AS mean_dense,
       (jr.result->>'mean_lexical_candidates')::numeric AS mean_lexical,
       (jr.result->>'mean_union_candidates')::numeric   AS mean_union,
       (SELECT count(*) FROM articles a
         WHERE a.site_id = jr.site_id AND a.is_active) AS active_articles
FROM job_runs jr
WHERE jr.kind = 'analysis'
  AND jr.result ? 'mean_union_candidates'
ORDER BY jr.id DESC;
```

Result on the dev database, 2026-08-19:

| site_id | mean_dense | mean_lexical | mean_union | active_articles |
| --- | --- | --- | --- | --- |
| 12 | 43.4 | 43.4 | 43.4 | 45 |
| 11 | 43.7 | 43.7 | 43.7 | 45 |
| 10 | 43.7 | 43.7 | 43.7 | 45 |
| 9 | 35.75 | 35.75 | 35.75 | 37 |

The three pool sizes are equal on every run. Each equals the active article count less the source
article and the targets the eligibility predicate removes. No eligible target is outside the union.
Retrieval loss on this data is zero.

This is a property of small corpora, not a property of the ranker. A site with more than 100 eligible
targets will show `mean_union` below its corpus size, and retrieval loss becomes possible again. The
query above is the standing check. It needs no new code and no new table.

### The ordering decision is large and unmeasured

Every `hybrid_bm25` row stores the rank that dense retrieval gave the same target
(`app/ml/hybrid.py:105`, `app/ml/candidate_ordering.py:161-162`). Comparing that rank against delivery
measures how far BM25-512 moves a target away from the cosine order. This query writes nothing.

```sql
SELECT count(*) FILTER (WHERE max_dense_rank <= 3) AS sources_matching_cosine_top3,
       count(*)                                    AS sources_total,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY max_dense_rank) AS median_worst_dense_rank,
       max(max_dense_rank)                         AS worst_dense_rank
FROM (SELECT source_article_id,
             max((score_components->>'dense_rank')::int) AS max_dense_rank
        FROM suggestions
       WHERE method = 'hybrid_bm25'
         AND score_components->>'dense_rank' IS NOT NULL
       GROUP BY 1) t;
```

Result on the dev database, 2026-08-19: 11 sources of 172 received the cosine top three. The median
source received a target at cosine rank 12. The worst received a target at cosine rank 42.

Across all 572 stored rows, 311 came from cosine rank 1 to 5, 119 from rank 6 to 10, and 142 from rank
11 or lower. Every row carries both a `dense_rank` and a `lexical_rank`, which confirms that no
delivered target came from one retriever alone.

### The graph signal cannot break the tie

Graph reranking is in shadow mode and changed no order in the two most recent runs
(`graph_reordered_sources: 0`, job runs 221 and 222). The reason is visible in the stored components:
494 of 511 rows carry `opportunity: 1.0`. The dev sites hold 8 active internal links in total, so almost
every target is an orphan and the opportunity score is constant. The graph feature has no discriminating
power on this data. Do not treat it as an ordering answer.

### What to run when a larger site exists

`scripts/run_evaluation.py` already accepts `--k` and already scores lexical, dense, and hybrid on one
frozen split (`scripts/run_evaluation.py:61`, `:127-139`). For a site whose eligible corpus exceeds 100
targets:

```
uv run --no-sync python scripts/run_evaluation.py \
  --split <frozen split for that site> --k 200 \
  --output docs/data/evaluation-pool-recall-<date>.json
```

`--k 200` is the maximum useful value without a code change. `run_evaluation.py:127-129` does not pass
`limit` to `rank_all`, so the rankings are truncated at `HYBRID_POOL_SIZE`, which is 200
(`app/ml/hybrid.py:51`). Recall@200 for the hybrid method is exactly the union pool recall, because
`rank_hybrid_candidates` only ever considers the union of the two 100-slot pools
(`app/ml/hybrid.py:202-222`). Subtracting Recall@5 from Recall@200 gives the ordering loss directly. The
metrics module states the same rule in its own docstring: high recall with low NDCG means retrieval
works and ordering does not (`app/ml/evaluation/metrics.py:15-16`).

## Evidence: where the rerank stage inserts

The call graph today is:

```text
generate_suggestions
  -> HybridRanker.rank            app/ml/hybrid.py:372-481   (builds the union, BM25-first order)
  -> order_candidates             app/ml/candidate_ordering.py:103-195
       -> score floor                   :131-135
       -> deterministic_rerank          :137-144   (graph, shadow by default)
       -> rerank_with_editorial_feedback :150-155  (off unless enabled per site)
       -> truncate to remaining         :156
       -> build score_components        :158-194
  -> Suggestion(...) insert       app/services/suggestion_service.py:496-516
```

The rerank stage belongs in `order_candidates`, after the score floor at line 135 and before the graph
call at line 137. That position keeps three existing properties. The hard eligibility rules have already
run in SQL. The site score floor has already removed rows the operator does not want. The evidence
builder at lines 158-194 is still downstream, so a new component is persisted by the same code path that
persists the graph and feedback components.

### Shadow plumbing

Two mechanisms already exist. Neither needs a migration.

**Per-row shadow evidence.** Write the reranker output as a new key in the components dictionary built at
`app/ml/candidate_ordering.py:160-178`, beside `graph` and `editorial_feedback`. Copy the graph
component's shape: it already carries `mode` and `applied`, so a shadow observation is distinguishable
from an applied one (`app/ml/candidate_ordering.py:98-99`). Do not overwrite `Suggestion.score`. Its
meaning is cosine similarity, and the dashboard, the thresholds, and the queue order all read it
(`app/models/suggestion.py:129-134`).

Provenance stays immutable by construction, and the constraint is stricter than the older note implies.
The migration `g4c5d6e7f8a9` installs `trg_suggestion_ranking_snapshot_immutable`, a
`BEFORE UPDATE OF score_components, retrieval_version, ranking_version, final_rank, feature_snapshot`
trigger that raises on any change
(`alembic/versions/g4c5d6e7f8a9_add_slice4_review_evidence.py:94-102`, `:259-262`). A rerank score
therefore cannot be back-filled onto an existing row. It must be written at insert time or not at all.
Record the reranker name and version in `ranking_version`, which is already composed per row
(`app/ml/candidate_ordering.py:64`).

**Run-level comparison with no row writes.** `generate_suggestions` accepts `comparison_only=True`,
which requires shadow mode, writes no suggestions, and returns overlap and exact-order rates in the job
result (`app/services/suggestion_service.py:231`, `:282-283`, `:373-374`, `:611-625`). The result lands
in `job_runs.result`, a JSONB column. This is the cheapest first experiment. It compares two orderings
over the same union and touches no editorial data.

### Which prefix to rerank

Do not take the top k of the BM25 order. The measurement above shows the BM25 order and the cosine order
disagree strongly, so a BM25 prefix discards candidates the dense retriever ranked first. Take the top k
by `fusion_rank`. It is the only stored order both retrievers contribute to (`app/ml/hybrid.py:204-206`,
`:102`), it is already computed, and it is already persisted in `score_components.fusion_rank`, so the
prefix choice is auditable after the fact.

## Evidence: which reranker is viable

The deployment constraint is fixed and recorded in the repository. The embedding worker runs on CPU
(`EMBEDDING_DEVICE=cpu` in `.env`, `app/config.py:84`) in a Docker VM of about 7.6 GB, and `bge-m3`
exhausts that memory at batch size 32. The model that runs today is `BAAI/bge-base-en-v1.5` at 0.1B
parameters and 768 dimensions
([bge-base-en-v1.5 model card](https://huggingface.co/BAAI/bge-base-en-v1.5)). Any reranker must fit
beside it.

| Model | Parameters | Max sequence | Licence | Languages | Verdict |
| --- | --- | --- | --- | --- | --- |
| `cross-encoder/ms-marco-MiniLM-L6-v2` | 22.7M | 512 (base is MiniLM-L12-H384) | Apache-2.0 | English | **Use first.** |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 0.1B | 512 | Apache-2.0 | 15 | Use for the multilingual route. |
| `BAAI/bge-reranker-base` | 0.3B | 512 | MIT | Chinese, English | Fallback if L6 accuracy is too low. |
| `BAAI/bge-reranker-v2-m3` | 0.6B | 512 | Apache-2.0 | multilingual | **Reject.** Built on `bge-m3`, which this deployment records as exhausting worker memory. |
| `mixedbread-ai/mxbai-rerank-base-v2` | 0.5B | not stated on the card | Apache-2.0 | 100+ | Reject for now. Same memory class as v2-m3. Its own card quotes 0.67 s on an A100. |
| `jinaai/jina-reranker-v2-base-multilingual` | 278M | 1024 | CC-BY-NC-4.0 | 26+ | **Reject.** The card restricts the weights to research and evaluation. LinkMesh is commercial. |
| LLM as reranker, through the existing OpenRouter path | not applicable | not applicable | vendor terms | multilingual | Final few rows only, never the union. |

The recommendation is `ms-marco-MiniLM-L6-v2`, and the tradeoff is stated plainly. It is 22.7M
parameters against `bge-reranker-base`'s 0.3B, so it is roughly one thirteenth that size and one fifth
the size of the embedding model already running. Its published quality is 74.30 NDCG@10 on TREC DL 2019,
which is within 0.01 of the L12 variant at nearly twice the throughput
([sentence-transformers pretrained cross-encoders](https://sbert.net/docs/cross_encoder/pretrained_models.html),
[ms-marco-MiniLM-L6-v2 model card](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2)). The
cost of the choice is that it is English-only and trained on MS MARCO web passages, not on editorial
prose. If the first shadow run shows no separation, the next step is `bge-reranker-base`, not a larger
multilingual model.

The two-stage design itself is what the vendors document. BAAI's own card describes retrieving the top
100 with an embedding model and reranking to the final three with a cross-encoder, and states that
rerankers are more accurate and less efficient
([bge-reranker-base model card](https://huggingface.co/BAAI/bge-reranker-base)). That is the shape
LinkMesh already has.

The LLM option needs one sentence. RankGPT shows that an instructed LLM can match supervised rerankers
zero-shot, and that the capability distils into a 440M model, but the authors state the efficiency
problem for real deployments explicitly
([Sun et al., *Is ChatGPT Good at Search?*](https://arxiv.org/abs/2304.09542)). LinkMesh already pays
one LLM call per opened suggestion for placement. Adding one per candidate would multiply that by the
union size. Keep the LLM where it is.

## Evidence: cost arithmetic

The counts below are exact. They come from the caps in the code and the measured union sizes.

Fixed quantities: `DENSE_POOL_SIZE = 100` and `LEXICAL_POOL_SIZE = 100`, so the union is at most 200
(`app/ml/hybrid.py:43-44`, `:51`). `hybrid_max_sources_per_run = 50` (`app/config.py:100`).
`hybrid_max_suggestions_per_article = 3` (`app/config.py:88`). The embedding stage already batches at 32
(`app/services/suggestion_service.py:31`).

| Scope | Pairs per source | Pairs per 50-source run | Batches of 32 |
| --- | --- | --- | --- |
| Whole union, dev sites today (measured 43.4) | 43.4 | 2,170 | 68 |
| Whole union, corpus above 200 (ceiling) | 200 | 10,000 | 313 |
| Top 20 by `fusion_rank` (bounded) | 20 | 1,000 | 32 |
| Top 50 by `fusion_rank` (bounded) | 50 | 2,500 | 79 |

Wall time is `pairs / T`, where `T` is the CPU throughput in pairs per second for the chosen model at
the chosen sequence length. No primary source gives a CPU figure. The published 1,800 documents per
second for `ms-marco-MiniLM-L6-v2` was measured on a V100 GPU, which its model card states. Rather than
guess `T`, invert the budget. To hold the rerank stage of one 50-source run under 120 seconds, `T` must
be at least:

- 8.3 pairs per second at top 20;
- 20.8 pairs per second at top 50;
- 18.1 pairs per second at the measured dev union;
- 83.3 pairs per second at the 200-candidate ceiling.

The top-20 requirement is the one to design against, because it does not grow with the corpus. Measure
`T` once with a throwaway timing script before writing the stage. Do not size the batch from this note.

One sequence-length constraint follows from the table above. Both recommended models cap at 512 tokens.
The reranker input must therefore be the source passage, the anchor span, the target title, and a short
target evidence window. It cannot be the whole source article, which the placement prompt reads at up to
12,000 characters (`app/config.py:74`).

## Evidence: anchor and placement quality

### Signals available before generation

The placement model is the only component that judges an anchor today, and it runs lazily, once per
opened suggestion (`app/api/routes/suggestions.py:1232-1310`). These signals are available at ranking
time and cost no model call.

- **Literal anchor availability.** The anchor must be a contiguous substring of a passage that is a
  substring of the first `placement_max_source_chars` of the source
  (`app/services/placement_service.py:125-130`, `:200`, `:209-210`). Whether any token n-gram of the
  target title occurs literally in that window is a pure string test on data already in memory. When it
  does not, the model must build an anchor from other words, and refusal becomes more likely.
- **Anchor length window.** A valid anchor is 2 to 120 characters and must be shorter than its passage
  (`app/services/placement_service.py:34-35`, `:134-142`). Candidate spans can be filtered against the
  same bounds before generation.
- **Anchor contention.** `taken_anchors` is both a prompt hint and a hard rejection
  (`app/services/placement_service.py:184-189`, `:211-213`, `app/api/routes/suggestions.py:1272-1281`).
  A run proposes up to three targets for one source at once, so contention is predictable at ranking
  time. It is currently discovered only at generation time.
- **Target evidence.** The target preview is the first 600 normalized characters
  (`app/services/placement_service.py:31`, `:156`). The same window is the natural target side of a
  reranker pair.

### Measured placement outcomes

Of 572 stored suggestions, 22 have had a placement generated, and 17 of those produced a usable anchor.
That is 5 refusals or verification failures in 22 attempts. The sample is far too small to act on. It is
reported here only to establish that the base rate is neither near zero nor near one. It cannot support
a threshold.

### What the reason codes actually record

The older note states that the API distinguishes six rejection reasons and advises preserving them as
separate outcomes. The schema is real: `RejectionReason` is a seven-value literal including `other`
(`app/schemas/suggestion.py:22-30`), the column exists (`app/models/suggestion.py:187`), the review
routes carry it (`app/api/routes/suggestions.py:865`, `:934`, `:1357`), the lifecycle event records it
in `details` (`app/services/evaluation_service.py:271`), and the dashboard offers the choices
(`linking-engine-frontend/src/components/suggestions/RejectionReasonDialog.tsx:4-16`).

Three things the older note does not say, and which change the P2 item:

1. **There is no reason-coded data.** The dev database holds one rejected suggestion. Its reason is
   `other`. There are four `reviewed` events in total, and only one of them carries a `rejection_reason`
   field at all. The gate on this work is not only the 100-label threshold. It is also whether reviewers
   use the dialog.
2. **The frozen label artifact discards the reason.** `ReviewerLabelExample` carries the label, the
   ranking snapshot, the exposure fields, and the score. It does not carry `rejection_reason`
   (`app/ml/evaluation/reviewer_labels.py:71-97`). Preserving reasons as separate outcomes requires a
   schema change to that artifact, which the older note does not identify.
3. **The reasons are not symmetric.** `bad_anchor` and `bad_placement` can only be chosen by a reviewer
   who saw a placement. Only 22 of 572 rows have one. `wrong_target` and `not_relevant` are available on
   every row. Any diagnostic that compares reason rates must condition on whether a placement was
   generated. If it does not, it will read the exposure pattern as a quality signal.

Google's guidance is the right editorial standard for the anchor itself. Link text should be
descriptive, reasonably concise, and supported by its surrounding context, and chains of adjacent links
should be avoided. Google states no ideal link count
([Google Search Central, crawlable links](https://developers.google.com/search/docs/crawling-indexing/links-crawlable)).
This is editorial guidance. It is not evidence of a ranking outcome.

## Prioritized improvements

### P0 - reranker in `order_candidates`, comparison-only

Insert a rerank stage in `app/ml/candidate_ordering.order_candidates` between line 135 and line 137.
Score the top 20 of the union by `fusion_rank`. Use `cross-encoder/ms-marco-MiniLM-L6-v2`. Shape the
pair as source title plus source passage against target title plus the 600-character target preview.
Keep the whole input under 512 tokens.

Run it first through `generate_suggestions(comparison_only=True)`, which writes no suggestion rows and
returns overlap statistics in `job_runs.result` (`app/services/suggestion_service.py:282-283`,
`:611-625`). Only after that, persist a per-row shadow component beside `graph`, with `mode` and
`applied` fields copied from the graph component's shape. Never write to `Suggestion.score`.

### P0 - measure the rerank throughput before writing the stage

Time the model once on this CPU at the intended sequence length. The arithmetic above is exact in model
calls. The wall time is not. The top-20 bound was chosen because it does not grow with the corpus. If
`T` is below 8.3 pairs per second, reduce the prefix before reducing the model.

### P1 - a pre-generation anchor availability check

Compute, for each candidate, whether any token n-gram of the target title occurs literally in the source
window the placement model will read. Record it as a component. Do not filter on it yet. Compare it
against the placement outcome once enough placements exist. This is a string test with no model cost. It
produces the first evidence that placement success is predictable at ranking time.

### P1 - carry the rejection reason into the frozen label artifact

Add `rejection_reason` to `ReviewerLabelExample` (`app/ml/evaluation/reviewer_labels.py:71-97`) and
raise its `SCHEMA_VERSION` (`:36`). Do this before the label gate is met, not after, so the labels
collected between now and the gate are usable. This is a schema addition to an artifact. It is not a
change to ranking.

### P1 - mild MMR for the final three

Unchanged from the 2026-08-17 note. Apply it after a relevance floor, inside the final small set only,
and keep relevance dominant ([Goldstein and Carbonell, MMR](https://aclanthology.org/X98-1025/)).

### P2 - multilingual route, per site

Unchanged in direction, changed in model. Use `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` for the
shadow route on a non-English site. It is 0.1B parameters, the same class as the embedding model already
running, and Apache-2.0. Do not change every site's model at once.

### P2 - deterministic source rotation in the production path

The production loop takes sources in ascending id order and stops at 50
(`app/services/suggestion_service.py:315`, `:377-380`). Shadow runs already spread their sources
(`:216-222`). Consider reusing `_evenly_spaced_ids`, or ordering by last-analyzed time, for the
production path. Measure source coverage before changing it.

### Deprioritized - graph reranking

Leave it in shadow. On the current sites it reorders nothing, and 494 of 511 rows carry the same
opportunity value. It is not a quality lever until the sites have a real link graph.

## Safe experiment sequence

Every step stays comparison-only until the existing reviewer-label gate is met
(`docs/design/slice5-reviewer-label-evidence.md:21-31`).

| Order | Experiment | Evidence required |
| --- | --- | --- |
| 1 | Time `ms-marco-MiniLM-L6-v2` on this CPU at 512 tokens, batch 32 | Pairs per second. Decides the prefix size before any code lands. |
| 2 | `comparison_only=True` run with the rerank stage, one site | Overlap at 3, exact-order rate, added wall time per run. No rows written. |
| 3 | Per-row shadow component, all four sites | Rank movement against `dense_rank` and `fusion_rank`, per site. Stable `Suggestion.score`. |
| 4 | Pre-generation anchor availability component | Correlation with the placement found rate, conditioned on placement having been generated. |
| 5 | Relevance floor plus MMR on the final three | Distinct-target rate, near-duplicate rate, target concentration, with relevance stable. |
| 6 | Reviewer-label benchmark, once the gate is met | nDCG@3, approved precision, per-site stability, paired wins and losses. |

Step 6 needs the label gate. The dev database currently holds four `reviewed` events. The gate is 100
eligible individual exposed labels on each of three sites. State that distance in any plan that depends
on it.

For every result, preserve the frozen dataset identity, the candidate-pool identity, per-site metrics,
confidence intervals, and paired wins and losses. TREC's rule still governs. The judgments must match
the collection being evaluated
([NIST relevance-judgment guidance](https://trec.nist.gov/data/reljudge_eng.html)).

## Keep, change, avoid

Keep: the hard eligibility predicate in one SQL string; the exact source-text placement verification;
the ranking-snapshot immutability trigger; mandatory human review; `Suggestion.score` meaning cosine
similarity; the dense and BM25 union for recall; graph shadow mode.

Change: the final ordering, inside `order_candidates`; the rerank prefix, taken by `fusion_rank`; the
frozen label artifact, to carry the rejection reason; the production source rotation.

Avoid: inserting a reranker in `app/ml/hybrid.py`; using `BAAI/bge-reranker-v2-m3` or any 0.5B-class
reranker on this CPU deployment; using `jina-reranker-v2` under its non-commercial licence; back-filling
a rerank score onto an existing row, which the immutability trigger rejects; treating the 2026-08-12
artifact as a re-runnable baseline; calling an LLM for every candidate during generation; and reading
the current reason-code distribution as a quality signal when it holds one row.

## Verification limits

- No application code, frontend code, database rows, migrations, or publication behavior was changed.
  Every database statement executed for this note was a `SELECT`.
- No model was downloaded, loaded, or timed. Every throughput number quoted comes from a vendor model
  card and was measured on GPU hardware. The CPU throughput `T` for this deployment is unmeasured. Step
  1 of the sequence exists to measure it.
- The dev-database numbers describe four small WordPress sites with 37 to 45 active articles each and 8
  active internal links in total. Conclusions about retrieval loss hold for corpora of this size. They
  do not transfer to a site with more than 100 eligible targets. The telemetry query above is the check.
- The placement outcome rate rests on 22 generated placements. It establishes an order of magnitude and
  nothing more.
- The reason-code conclusion rests on one rejected suggestion and four review events.
- `docs/data/evaluation-baselines-2026-08-12.json` was read but could not be reproduced. Its site ids
  are absent from the current database.
- The claim that a 0.5B-parameter reranker will exhaust memory on this worker is an inference. The
  direct evidence is the `.env` record for `bge-m3` and the shared parameter count. It was not tested.
- Docker was reachable in this pass, unlike the 2026-08-17 pass. No service was started or stopped.
- The working tree is `feat/dashboard-auth` at commit `955e774`, with unrelated uncommitted changes in
  the alerts, graph, and job-service modules. None of them touch ranking, scoring, or placement.

## Primary-source index

- https://huggingface.co/BAAI/bge-base-en-v1.5
- https://huggingface.co/BAAI/bge-reranker-base
- https://huggingface.co/BAAI/bge-reranker-v2-m3
- https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2
- https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
- https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual
- https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v2
- https://sbert.net/docs/cross_encoder/pretrained_models.html
- https://sbert.net/docs/cross_encoder/usage/usage.html
- https://arxiv.org/abs/2304.09542
- https://arxiv.org/abs/1901.04085
- https://aclanthology.org/X98-1025/
- https://developers.google.com/search/docs/crawling-indexing/links-crawlable
- https://trec.nist.gov/data/reljudge_eng.html
