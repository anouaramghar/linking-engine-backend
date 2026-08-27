# LinkMesh suggestion quality research - 2026-08-17

Status: research only. No application code or publication behavior was changed.

Question: can LinkMesh make internal-link suggestions more relevant and more realistic for an editor?

## Conclusion

Yes. The strongest next experiment is a bounded second-stage ranker that judges the proposed edit in context:

```text
source passage + literal anchor span + target evidence
```

Keep the current dense/BM25 candidate union for recall, then test a cross-encoder or small feature ranker over the best candidates. Add a cheap placeability/anchor-fit check, select the final three with a mild redundancy penalty, and keep the whole change comparison-only until the existing reviewer-label gate is met.

This is an inference from the sources and the current LinkMesh contracts. It is not a claim that a particular model will improve live results.

## What LinkMesh already does well

- The production path embeds missing articles, builds one Hybrid ranker snapshot, and preserves generation-time ranking evidence (`app/services/suggestion_service.py:243-307`, `:474-515`).
- Hybrid retrieval keeps a dense top-100 pool and a lexical top-100 pool. The lexical recipe gives title terms weight 3, taxonomy terms weight 2, and uses the first 512 body tokens (`app/ml/hybrid.py:43-57`, `:131-140`).
- Candidate eligibility is strong: active/safe targets, minimum semantic score, duplicate and near-duplicate suppression, existing-link suppression, prior-decision suppression, reverse-link suppression, and low-value-target rules (`app/ml/baseline.py:102-150`, `:186-263`). These filters should remain hard rules.
- Every normal run is capped at three active suggestions per source and 50 sources per run. The queue remains review-first (`docs/design/global-hybrid-ranking.md:7-20`).
- Placement is extractive and verified: the model must quote the source, choose a contiguous anchor, and can decline when no natural fit exists (`app/services/placement_service.py:37-62`, `:114-144`). The stored passage is checked against the source before it is shown or published (`app/services/placement_service.py:176-221`).
- Ranking and review provenance are already immutable, and exposed individual labels are separated from unseen/bulk decisions (`docs/design/suggestion-traceability.md:31-42`, `docs/design/slice5-reviewer-label-evidence.md:6-31`).

## The current quality gap

### The final order is still lexical-heavy

The Hybrid union broadens recall, but `rank_hybrid_candidates` sorts the union by raw BM25 score and uses fusion rank only as a tie-breaker (`app/ml/hybrid.py:185-233`). The stored `score` remains cosine similarity, so it is not the value that decides the delivered order.

That is a clean seam for a reranker, but it means the current system does not yet judge whether a target is useful in the exact sentence where a link would appear.

The saved 2026-08-12 artifact also shows lexical and Hybrid results with the same all-site Recall@5, NDCG@5, and nearly identical MRR (`docs/data/evaluation-baselines-2026-08-12.json:2-66`, `:134-198`). This is historical observed-link proxy evidence, not current-live or promotion-grade evidence; it is consistent with testing a true post-retrieval reranker rather than another retrieval blend.

### Placement is evaluated late

Placement is generated lazily when an editor opens a suggestion or during bounded publication preparation (`app/api/routes/suggestions.py:1237-1301`, `app/services/publication_plan_service.py:250-354`). The prompt sees up to 12,000 source characters and only a 600-character normalized target preview (`app/config.py:70-77`, `app/services/placement_service.py:28-35`, `:147-173`).

This makes the placement output trustworthy when it exists, but placement success does not currently influence candidate ranking. A pair can therefore rank highly and later return “no natural spot”.

### Graph and editorial feedback are not the main answer yet

Graph reranking defaults to `shadow`, so opportunity signals are observed without changing production order (`app/config.py:219-229`). Editorial feedback is opt-in, score-bucket based, and its own code calls the sample floor a floor rather than proof (`app/services/editorial_feedback.py:60-104`, `:121-160`). It can tune score policy, but it cannot tell whether a particular source passage and target form a natural edit.

### Language and local context may be underrepresented

The committed default embedding model is `BAAI/bge-base-en-v1.5` (`app/config.py:82-84`), while the lexical tokenizer keeps only `[a-z0-9]` terms and English stopwords (`app/ml/lexical.py:8-51`). If a site is French, Arabic, or otherwise non-English, this is a plausible quality ceiling. Runtime overrides must be checked before changing this conclusion.

The dense input is `title + full content_text`, while BM25 sees title, taxonomy, and only the first 512 body tokens (`app/services/suggestion_service.py:111-123`, `app/ml/hybrid.py:131-140`). Keep article-level retrieval for recall, but add local sentence/paragraph evidence for final ranking.

## Primary-source findings

1. A two-stage retrieval architecture is well established: retrieve a broad set cheaply, then apply a more expensive relevance model to a bounded candidate set. The original BERT passage-reranking paper explicitly describes this separation and scores a query-passage pair ([Nogueira and Cho, *Passage Re-ranking with BERT*](https://arxiv.org/abs/1901.04085)). BAAI's first-party model card documents the same pair-scoring interface for its rerankers ([BGE reranker model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)).

2. Link recommendation quality is about the target and the source location together. Wikimedia's Add-a-link research models a target article plus an existing anchor/mention and uses a threshold to trade precision against recall ([Wikimedia Add-a-link research](https://meta.wikimedia.org/wiki/Research%3ALink_recommendation_model_for_add-a-link_structured_task)). Anchor Prediction finds that useful target evidence can require joint reasoning over source and target pages, including related-but-not-redundant passages ([Liu, Lee, and Toutanova, *Anchor Prediction*](https://arxiv.org/abs/2305.14337)).

3. Natural link text should be descriptive, concise, relevant, and supported by surrounding context. Google also warns against forced keyword stuffing and chains of adjacent links; it gives no universal ideal link count ([Google Search Central link best practices](https://developers.google.com/search/docs/crawling-indexing/links-crawlable)). This is editorial guidance, not proof of an SEO outcome.

4. Maximal Marginal Relevance (MMR) is a principled way to retain relevance while penalizing redundancy in a small selected set ([Goldstein and Carbonell, *Using MMR for Diversity-Based Reranking*](https://aclanthology.org/X98-1025/)). For LinkMesh, this belongs after a relevance floor and only within the final small batch.

5. Unseen or implicitly clicked items are not clean negatives. Position/exposure bias can distort learning-to-rank signals ([Joachims, Swaminathan, and Schnabel, *Unbiased Learning-to-Rank with Biased Feedback*](https://arxiv.org/abs/1608.04468)). Link-recommendation research likewise finds that exposure bias can distort evaluation and create feedback loops, while exposure-aware methods can improve diversity ([Gupta et al., *Correcting Exposure Bias for Link Recommendation*](https://proceedings.mlr.press/v139/gupta21c.html)). LinkMesh's exposed individual reviewer-label contract is therefore the right foundation.

6. Neural ranker outputs should not be treated as calibrated probabilities automatically. Research finds BERT-based rankers can be poorly calibrated and that uncertainty can help risk-aware ranking ([Penha and Hauff, *On the Calibration and Uncertainty of Neural Learning to Rank Models*](https://arxiv.org/abs/2101.04356)). Temperature scaling is a later calibration option, not a substitute for LinkMesh labels ([Guo et al., *On Calibration of Modern Neural Networks*](https://arxiv.org/abs/1706.04599)).

## Prioritized improvements

### P0 - shadow cross-encoder reranking

Test a cross-encoder over the current Hybrid union before the final BM25 order. Start with a bounded top-20 or top-50 per source; keep the dense/BM25 top-100 pools unchanged so the experiment isolates ordering quality.

Use inputs shaped like:

```text
source title + source sentence/paragraph
target title + target lead, heading, or evidence passage
```

Compare the current BM25-512 order with an English/CPU candidate and a multilingual candidate such as `BAAI/bge-reranker-v2-m3`. This is a model shortlist, not a production recommendation. The model card supports direct pair scoring and multilingual use; it does not prove improvement on LinkMesh data.

Persist a separate `pair_relevance` component and ranking version. Do not overwrite `Suggestion.score`, because its current meaning is cosine similarity and the dashboard depends on that contract.

### P0 - rank the complete proposed edit

Add a cheap candidate layer that extracts source sentences/paragraphs and possible contiguous anchor spans. Score or filter:

- exact anchor existence and contiguity;
- target-title/entity ambiguity;
- target evidence versus the local source passage;
- generic or forced anchor penalties;
- whether the source already covers the concept;
- whether another proposed link is too close in the same sentence or passage.

Use the existing placement LLM only for the final few candidates or the selected rows. Keep its current verbatim verification and refusal behavior. A high article-level score must not rescue an invalid anchor or a source passage with no natural fit.

Add a target evidence excerpt to the review surface so the editor can see why the target helps, not only the target title and a semantic-match percentage. The current queue shows source/target titles and semantic match (`src/components/suggestions/SuggestionCard.tsx:64-105`), while placement context is a separate lazy card (`src/components/suggestions/PlacementContextCard.tsx:60-78`).

### P1 - mild MMR selection for the final three

After relevance filtering, select up to three targets while penalizing target-target near-duplicates, repeated topic/taxonomy coverage, and excessive concentration on one target. Keep relevance dominant; MMR must not promote a clearly weak target merely to create variety.

Measure distinct-target rate, near-duplicate rate, target/topic concentration, and taxonomy coverage alongside nDCG and approved precision. Do not introduce a guessed global diversity threshold before measuring site distributions.

### P1 - local context and language routing

Keep whole-article dense retrieval as the recall layer, but compute a best local source window and a target evidence window for reranking. Do not replace the existing article baseline with fixed chunks without a fresh comparison.

For non-English sites, run a language-aware shadow route. The current English tokenizer and English embedding default are a likely weakness for multilingual content. Compare a multilingual embedding/reranker route per site instead of changing every site's model at once.

### P2 - learn from reason-coded reviewer labels

The API already distinguishes `not_relevant`, `wrong_target`, `bad_anchor`, `bad_placement`, `already_covered`, and `duplicate` (`app/schemas/suggestion.py:20-30`). Preserve those as separate outcomes. The current reviewer benchmark is deliberately binary and evidence-gated; extend it only after the current three-site/100-eligible-label-per-site gate is met (`docs/design/slice5-reviewer-label-evidence.md:21-31`).

Use approved/rejected labels for the first ranking benchmark, then add multi-task or reason-specific diagnostics. Never treat unseen rows or bulk decisions as negative relevance labels. If implicit interactions are added later, retain position and exposure and use an exposure-aware method.

### P2 - calibrated abstention, not a bigger confidence number

Keep raw cosine, BM25, and reranker values for traceability. After enough labels, fit a held-out acceptance estimate using site, language, and content-type fallbacks. Combine it with top-1/top-2 margin and placement fit. If evidence is weak or the local fit is absent, return “needs review” or skip the candidate instead of manufacturing a precise probability.

### P2 - improve source-batch coverage

The generation loop orders source IDs ascending and stops after the configured source limit (`app/services/suggestion_service.py:309-319`, `:363-379`). That bound protects review capacity, but it may repeatedly privilege older/low-ID sources. Test a deterministic rotation by last-analyzed time or source strata; measure source coverage and acceptance before changing it.

## Safe experiment sequence

All steps remain shadow/comparison-only until the existing evidence gate is satisfied.

| Order | Experiment | Evidence required |
| --- | --- | --- |
| 1 | Candidate diagnostic: dense, BM25, and union Recall@100/@200 by site and source type | Identify retrieval loss versus ordering loss. |
| 2 | Cross-encoder over the existing union | nDCG@3/@5, MRR, approved precision, rejected rate, per-site stability, CPU/latency. |
| 3 | Add local context and literal-anchor features | Lower `wrong_target`, `bad_anchor`, and `bad_placement` without unacceptable relevance loss. |
| 4 | Add relevance-floor + MMR final selection | Lower redundancy and concentration with stable relevance. |
| 5 | Add calibration/abstention | Reliability and risk-coverage results on a held-out label slice. |

### Recommended scorecard

- Retrieval ceiling: candidate Recall@100 and Recall@200.
- Queue quality: nDCG@3, nDCG@5, MRR, approved-hit recall, approved precision, and rejected-label rate.
- Realism: valid contiguous anchor rate, natural-placement rate, accepted in-text placement rate, and reason-specific rejection rates.
- Set quality: duplicate/near-duplicate rate, target concentration, target/topic novelty, and source/target/taxonomy coverage.
- Trust: calibration error for any acceptance probability, reviewer disagreement, and abstention risk.
- Operator value: time to decision, undo/re-review rate, and publication fallback-to-appended-block rate.
- Operations: model latency, CPU/RAM cost, placement calls, and failure/retry rate.

For each result, preserve the frozen dataset identity, candidate-pool identity, per-site metrics, confidence intervals, and paired wins/losses. TREC's evaluation guidance captures the core rule: judgments must match the collection being evaluated ([NIST relevance-judgment guidance](https://trec.nist.gov/data/reljudge_eng.html)).

## Keep, change, avoid

Keep: hard eligibility filters, exact source-text placement verification, immutable ranking provenance, mandatory human review, the current cosine-score meaning, and graph shadow mode.

Change: final ordering, local context evidence, anchor/placeability signals, small-set diversity, reason-coded evaluation, and later calibration.

Avoid: global embedding swaps based only on generic benchmarks, activating GNN as the primary cold-start solution, using raw model scores as confidence, training from unseen/bulk decisions, or making an LLM call for every candidate during generation.

## Verification limits

- No application code, frontend code, database rows, or publication behavior was changed.
- The saved evaluation artifact is historical observed-link proxy evidence, not a current promotion result.
- Live Docker inspection was not available in this pass because the local Docker Linux-engine named pipe denied access; that is an environment permission boundary, not evidence of an application failure.

## Primary-source index

- https://arxiv.org/abs/1901.04085
- https://huggingface.co/BAAI/bge-reranker-v2-m3
- https://meta.wikimedia.org/wiki/Research%3ALink_recommendation_model_for_add-a-link_structured_task
- https://arxiv.org/abs/2305.14337
- https://developers.google.com/search/docs/crawling-indexing/links-crawlable
- https://aclanthology.org/X98-1025/
- https://arxiv.org/abs/1608.04468
- https://proceedings.mlr.press/v139/gupta21c.html
- https://arxiv.org/abs/2101.04356
- https://arxiv.org/abs/1706.04599
- https://trec.nist.gov/data/reljudge_eng.html
