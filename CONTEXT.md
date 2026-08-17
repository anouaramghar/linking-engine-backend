# LinkMesh Content Context

This context names the facts and lifecycle of content collected from a site so
connectors and ingestion workflows use the same language.

## Crawl lifecycle

**Crawl snapshot**:
A complete-enough set of article and discovery observations from one ingestion
run that can safely replace the site's active content view. _Avoid_: crawl
delta, database backup.

**Promotion**:
The success-gated act of making a crawl snapshot the active content view after
its completeness and relationship checks pass. _Avoid_: publish, commit.
