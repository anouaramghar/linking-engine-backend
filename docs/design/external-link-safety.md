# External-link safety

Every managed site owns an outgoing external-link policy. External targets can
come from two sources with deliberately different contracts:

| Target origin | Stored as | Discovery rule |
| --- | --- | --- |
| Content pool | A reusable `Article` owned by an approved pool `Site` | Participates in the normal candidate corpus and deterministic trust score |
| Dynamic web search | A direct URL on a `Suggestion` with method `external_search` | Tavily is used only to fill slots left after the normal candidate pipeline |

The policy is applied before ranking, whenever the policy changes, and again
immediately before publication. The publication check is the final gate: a row
approved under an older policy cannot bypass a newer block.

## Content-pool hard guards

A content-pool target is never eligible when any of these conditions is true:

- external suggestions are disabled for the managed site;
- the target belongs to a content-pool source that is unapproved or quarantined;
- HTTPS is required and the target URL is not HTTPS;
- the target domain is on the site's blocklist or competitor list;
- the target domain belongs to any managed site (owned-domain isolation);
- a minimum domain age is configured and the real registration date is unknown
  or too recent;
- the resulting trust score is below the site's minimum.

Blocklist, competitor, and owned-domain decisions take precedence over an
allowlist match. Domain rules match both the named domain and its subdomains.

## Content-pool trust score

The deterministic score is capped at 100:

| Signal | Points |
| --- | ---: |
| HTTPS target | 30 |
| Trusted TLD, or no TLD restriction | 15 |
| Approved and non-quarantined pool source | 15 |
| Explicit domain allowlist match | 20 |
| No allowlist configured (neutral default) | 10 |
| Domain age under 30 days | 5 |
| Domain age 30-179 days | 10 |
| Domain age 180-364 days | 15 |
| Domain age at least 365 days | 20 |

Domain age is operator-supplied as the source's real registration date. An
unknown date remains unknown; LinkMesh never substitutes the date the source
was connected. If an RSS item points to a different domain than its source,
that item's domain age is also unknown and is evaluated accordingly.

The evaluation is stored in each content-pool suggestion's `score_components`
as `external_trust`, making the score and individual checks visible in the
review drawer and available for later audits.

## Tavily fallback boundary

Dynamic search is a paid gap-filler, not a second primary ranker. For each source
article, the analysis pipeline first selects eligible internal and content-pool
candidates. It calls Tavily only when all of the following are true:

- at least one suggestion slot is still open;
- the site-wide queue still has capacity;
- the run is creating suggestions rather than running comparison-only analysis;
- the source title is non-empty;
- the site's external-link policy has external links **enabled**; and
- `TAVILY_API_KEY` is configured.

The policy check is made before the request, not against its results. The search
is billed and it sends the source article's title to a third party, both at the
moment the request leaves; rejecting every candidate afterwards undoes neither.
A site with external links switched off therefore issues no outbound search at
all, and records one `external_links_disabled` audit event saying so.

The source title is the search query and is capped at 1,500 characters. Searches
are issued synchronously, one source at a time. The provider request uses:

- `search_depth=basic`, `topic=general`, and `auto_parameters=false`;
- at most `TAVILY_MAX_RESULTS_PER_REQUEST` results, hard-capped at 5;
- a default timeout of 10 seconds;
- no generated answer, raw page content, or images; and
- `include_usage=true` so consumed credits can be audited.

Owned domains plus the site's blocklist and competitor list are sent to Tavily
as provider-side exclusions, capped at 150 domains. Provider-side filtering is
only an optimization: LinkMesh repeats every safety check locally before a
candidate can be stored.

Transient transport failures, HTTP 429, and HTTP 5xx responses receive at most
two retries after 1 and 2 seconds (three attempts total). Quota/plan responses
(HTTP 432/433), other client errors, and malformed successful responses are not
retried. These bounds follow Tavily's
[search best practices](https://docs.tavily.com/documentation/best-practices/best-practices-search)
and [Search API contract](https://docs.tavily.com/documentation/api-reference/endpoint/search).

## Dynamic web-search hard guards

A Tavily candidate is never eligible when any of these conditions is true:

- external suggestions are disabled for the managed site;
- the target is not HTTPS;
- the target domain is on the site's blocklist or competitor list;
- the target domain belongs to any managed site;
- the URL is invalid or longer than the storage bound after normalization; or
- the normalized URL duplicates an active direct external suggestion for the
  same source article or another result in the same response.

The content-pool source-approval, trusted-TLD, domain-age, allowlist, and trust
score rules are not applied to direct web-search results. A Tavily result is not
a pool source and has no operator-supplied registration metadata. Treating it as
if it had those attributes would create false trust evidence.

Candidates that pass the hard guards are embedded from their title and snippet.
Their cosine similarity to the source article must satisfy both the global
`SUGGESTION_MIN_SCORE` and the site's editorial minimum. LinkMesh ranks them by
that semantic score, using Tavily rank only as a deterministic tie-breaker, and
stores no more than the number of open slots. `provider_score` remains separate
provider trace data; it is never presented as LinkMesh semantic confidence.

## URL identity and storage

External URLs are normalized before storage: scheme and host case, IDN host,
default ports, fragments, tracking parameters, and query ordering are made
canonical. Deduplication uses that canonical URL, so aliases differing only in
visit-tracking data become one target.

A dynamic web-search result is not imported into the content pool. An accepted
row has `target_article_id=NULL` and stores `external_url`, `external_title`,
`external_snippet`, provider provenance, and the search query directly on the
suggestion. A database constraint requires every suggestion to have exactly one
target form: an article id or a direct external URL, never both.
