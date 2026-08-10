# External-link safety

Every managed site owns an outgoing external-link policy. The policy is applied
to content-pool targets before ranking, whenever the policy changes, and again
immediately before publication. The publication check is the final gate: a row
approved under an older policy cannot bypass a newer block.

## Hard guards

An external target is never eligible when any of these conditions is true:

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

## Trust score

The deterministic score is capped at 100:

| Signal | Points |
| --- | ---: |
| HTTPS target | 30 |
| Trusted TLD, or no TLD restriction | 15 |
| Approved and non-quarantined pool source | 15 |
| Explicit domain allowlist match | 20 |
| No allowlist configured (neutral default) | 10 |
| Domain age under 30 days | 5 |
| Domain age 30–179 days | 10 |
| Domain age 180–364 days | 15 |
| Domain age at least 365 days | 20 |

Domain age is operator-supplied as the source's real registration date. An
unknown date remains unknown; LinkMesh never substitutes the date the source
was connected. If an RSS item points to a different domain than its source,
that item's domain age is also unknown and is evaluated accordingly.

The evaluation is stored in each external suggestion's `score_components` as
`external_trust`, making the score and individual checks visible in the review
drawer and available for later audits.

## URL identity

External URLs are normalized before storage: scheme and host case, IDN host,
default ports, fragments, tracking parameters, and query ordering are made
canonical. Deduplication uses that canonical URL, so aliases differing only in
visit-tracking data become one article.
