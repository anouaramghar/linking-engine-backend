# Citation-need detection

## Scope

The first citation detector is an explainable local baseline. It answers one
question: which exact sentences in a managed source article contain claims that
normally need editorial evidence?

It does **not** decide that a proposed target proves the sentence. Candidate
relevance, external-link policy, and live-URL safety remain separate evidence.
It also does not block suggestion generation or publication. That separation is
intentional until editor labels are numerous enough to evaluate false positives
and false negatives.

No GNN, XGBoost, LLM, or external API is used. Article prose stays inside
LinkMesh. Tavily continues to receive the article title only.

## Detector contract

`app/services/citation_need.py` exposes `analyze_citation_needs`. The result is
content-addressed with SHA-256 and versioned as `citation_rules_en_v2`. Each
accepted sentence records:

- the exact sentence copied from the article;
- its start-inclusive and end-exclusive character offsets;
- a confidence between zero and one;
- every rule that contributed to the result;
- the detector version.

The default threshold is `0.65`. Analysis is bounded to 30,000 characters, 500
sentences, and the ten highest-confidence results. The API reports `truncated`
when either analysis bound cut the document and `total_detected` before applying
the requested result limit.

## Explainable signals

The English baseline recognizes six signal families, split by whether the
signal can qualify a sentence on its own:

| Signal | Examples | Weight | Kind |
| --- | --- | ---: | --- |
| Research or attribution | study, survey, according to, data shows | 0.85 | primary |
| Quantitative claim | percentages, currency, measured units | 0.78 | primary |
| Health or safety claim | risk, treatment, unsafe, infection | 0.70 | primary |
| Causal claim | causes, reduces, associated with | 0.60 | primary |
| Time-sensitive claim | years, currently, latest, as of | 0.62 | supporting |
| Comparative claim | higher, lower, largest, most | 0.55 | supporting |

A sentence qualifies only when at least one **primary** signal matches.
Supporting signals are ordinary English — "now", "more", "better" — that
co-occurs with claims without being one. In v1 the two supporting signals
combined to `0.829` and cleared the threshold by themselves, so sentences like
"Our team now offers more flexible scheduling" were flagged. They may now only
raise the confidence of a sentence a primary signal already selected.

Matching signals combine as independent evidence:

`confidence = 1 - product(1 - signal_weight)`

Questions, short fragments, and sentences that already contain an explicit URL
or numeric inline citation are excluded. Results are ordered by confidence and
then source position, which makes repeated runs deterministic.

The number is detector confidence, not factual confidence and not target
relevance. The dashboard states that distinction next to every result.

## API and suggestion evidence

`GET /api/v1/articles/{article_id}/citation-needs?limit=10` computes the bounded
result for an authorized managed article. Content-pool articles are target-only
and therefore rejected by this source-analysis endpoint.

During suggestion generation, LinkMesh runs the detector once per eligible source
article. If a sentence qualifies, the primary result is stored for internal,
Content Pool, and dynamic web-search suggestions inside the immutable generation
evidence at:

- `suggestions.score_components.citation_need`;
- `suggestions.feature_snapshot.citation_need`;
- the `external_discovered` suggestion event for Tavily results;
- the external-search audit event for Tavily requests.

Older suggestions and articles with no qualifying sentence simply have no
`citation_need` component. The queue remains backward compatible.

## Deliberate limitations

- The lexical rules are English, so an article whose language is positively
  identified as anything else is skipped: the result reports
  `language_supported: false` with no sentences analyzed, which distinguishes
  "not analyzed" from "nothing needs a source". A language of `None` or `und`
  stays analyzable on purpose — connectors that cannot detect a language store
  `None` (the WordPress connector does so explicitly for an English-only
  fleet), and skipping those would disable the feature everywhere.
- Plain article text cannot reliably tell whether HTML anchor text already
  cites a claim; only explicit URLs and bracketed numeric citations are skipped.
- A detected sentence is generation evidence, not a placement. The subsequent
  anchor-selection tasks may use the stored offsets, but this task does not pick
  an anchor or modify article HTML.
- Activation as a hard gate requires a labelled evaluation set and explicit
  pass criteria. The current baseline is observable without silently reducing
  the existing suggestion queue.
