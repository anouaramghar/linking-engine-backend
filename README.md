# LinkMesh backend

The LinkMesh engine crawls connected sites, generates internal-link suggestions,
supports operator review, and publishes approved links. FastAPI serves the API;
PostgreSQL stores durable state; Redis and RQ run ingestion, analysis, and publication
jobs.

## Local stack

## Review reliability and traceability

Large reversible review operations, exact partial-failure recovery, and their
API contracts are documented in
[docs/design/bulk-review-reliability.md](docs/design/bulk-review-reliability.md).
The searchable audit dashboard and publication-attempt history are documented
in [docs/design/suggestion-traceability.md](docs/design/suggestion-traceability.md).

## Evaluation dashboard history

The evaluation API computes editorial, placement, publishing, method, and site
metrics from live suggestion data. Date filters select a generated-suggestion
cohort, so every outcome on the page describes the same population.

Orphan-page history is prospective because an earlier crawl state cannot be
reconstructed after links change. After applying migrations, register the
idempotent daily snapshot job once:

```bash
python scripts/schedule_evaluation_snapshots.py
```

The worker must listen to the `default` queue with the RQ scheduler enabled, as
the provided Docker Compose worker does. Re-registering the script is safe: the
job id is unique, and each site/date snapshot is updated rather than duplicated.

## External-link safety

Managed sites have independent outgoing-link policies covering HTTPS, trusted
TLDs, domain age, allowlists, blocklists, and competitor domains. Owned domains
are always protected. The same policy is enforced before ranking and again
before publication. Approved content-pool sources provide reusable external
articles; when the normal internal/content-pool pipeline leaves open slots, a
configured Tavily provider can supply bounded direct-URL candidates as a paid
fallback. The separate safety rules, provider limits, ranking contract, and
storage model are documented in
[`docs/design/external-link-safety.md`](docs/design/external-link-safety.md).
Accepted Tavily suggestions join the normal lifecycle audit, while request and
candidate decisions also have a durable provider audit described in
[`docs/design/suggestion-traceability.md`](docs/design/suggestion-traceability.md).
The one-shot `pool-scheduler-init` Compose service automatically registers the
unique daily content-pool ingestion coordinator during deployment; repeated
deployments reuse the existing schedule.

## Running the tests
Create `.env` from `.env.example`, then start the services:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

The API health endpoint is `http://127.0.0.1:8000/api/v1/health`. Compose runs separate
workers for ingestion/default jobs, analysis, and publication so a long ML task cannot
block publishing or crawling.

## Dashboard authentication

The dashboard uses Telegram identity plus admin approval. A first `/start` records a
pending request. After an approved operator accepts it, a later `/start` returns a
short-lived one-time code which the operator enters in the dashboard. Configure:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-bot-token
TELEGRAM_BOT_USERNAME=replace-with-bot-username
DASHBOARD_SESSION_SECRET=replace-with-a-long-random-secret
DASHBOARD_BOOTSTRAP_ADMIN_ID=123456789
```

The bootstrap ID only promotes a pending user; restarting cannot restore a revoked operator.
Approved dashboard users intentionally have full internal access. Keep the service behind
the IP-restricted firewall; login is the second layer, not a replacement for the firewall.

Per-site API keys remain available for limiting credential blast radius. They are not a
human login or a client-isolation boundary. Content-pool sites remain admin-only even when
their tenant ID matches a scoped key.

See [dashboard authentication](docs/design/dashboard-authentication.md) and
[per-site authorization](docs/design/per-site-authorization.md) for the contracts.

## Publication

LinkMesh publishes only the exact article change a named operator saw and approved.
Reviewing a suggestion **selects** it; it does not schedule anything. Publication is
three separate calls, and only the middle one is a decision:

```http
POST /api/v1/publish/{site_id}/plans/prepare?max_articles=10
POST /api/v1/publish/{site_id}/plans/approve   {"plans": [{"id": 55, "plan_hash": "..."}]}
POST /api/v1/publish/{site_id}   {"plan_ids": [55]}
```

Preparation reads the live WordPress posts, makes every decision publication used to
make on its own — cohort, ordering, anchor arbitration, in-text or appended block, the
rendered HTML — and stores each source article's edit as an immutable `PublicationPlan`
with a SHA-256 hash over the whole artifact. It writes nothing back to WordPress.

Approval requires a dashboard session or an operator-specific key, names each plan by id
*and* by the hash the operator was shown, and is all-or-nothing. The worker then consumes
approved plans only: it recomputes the hash, and sends the stored HTML verbatim while the
live post still equals the approved original. If the article changed, the plan becomes
`stale`, nothing is written, and a new plan must be prepared and approved.

`POST /publish/{site_id}` returns 409 when the site has no approved plan. The approval
screen sends the exact visible plan IDs so it cannot queue an older hidden approval;
omitting the body intentionally queues every approved plan as a recovery action. Neither
form makes an approval decision, so both are safe to retry after a queueing failure.

**Existing selected suggestions are not grandfathered in.** They migrate with no plan
link and need preparation plus explicit approval.

WordPress core has no compare-and-swap on post updates, so a narrow race remains between
the final read and the POST. See
[immutable publication plans](docs/design/immutable-publication-plans.md) for the full
contract, the lifecycle, and the deployment order.

### A site the engine may write to

Preparation reads every source post with `context=edit`, which WordPress refuses for an
anonymous caller. A site with no application password is therefore refused up front with
a 409, and the queue says so before anyone opens a review — one message rather than one
failed live request per article.

Credentials can be attached to an existing site, so a revoked or rotated application
password no longer means deleting the site and losing its articles and review history:

```http
PUT    /api/v1/sites/{site_id}/credentials   {"wp_username": "editor", "wp_app_password": "…"}
DELETE /api/v1/sites/{site_id}/credentials
```

Publication cannot be exercised against a site the engine has no account on: preparation
reads each post with `context=edit`, and WordPress answers an anonymous caller with 401.
Connect a site you are allowed to write to, over HTTPS, with an application password for a
user who can edit posts.

`ALLOW_UNSAFE_CRAWL_TARGETS` relaxes the SSRF guard and the HTTPS-with-credentials rule for
*every* site in the environment, not just the one being tested. Leave it off. If a local
target ever makes it necessary, turn it off again as soon as that work is finished.

## Running tests

The suite writes to PostgreSQL and refuses to run against the development database. Use an
isolated database named `linkmesh_test`:

```powershell
docker compose up -d db redis
docker compose exec db psql -U linkmesh -d postgres -c "CREATE DATABASE linkmesh_test OWNER linkmesh"
$env:DATABASE_URL='postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'
alembic upgrade head
pytest -q
```

Full setup and CI notes are in [docs/testing.md](docs/testing.md).

## Credential encryption

`CREDENTIAL_ENCRYPTION_KEY` is the primary Fernet key for WordPress application
passwords. To rotate it, move the current key to `CREDENTIAL_DECRYPTION_KEYS`, deploy a
new primary key, run `alembic upgrade head`, verify ingestion and publication, then remove
the previous key. Multiple previous keys may be comma-separated. Never commit real keys.

## Operator-specific API keys

Automation that needs a named operator identity can use:

```dotenv
OPERATOR_API_KEYS={"alice":"replace-with-alice-key","bob":"replace-with-bob-key"}
```

Send the matching value in `X-API-Key`. The generic `API_KEY` remains suitable for service
operations but cannot perform content-pool approval/reactivation because it identifies no
person. Successful content-pool decisions create immutable audit events available at:

```http
GET /api/v1/sites/{site_id}/pool-source/audit-events?limit=50&offset=0
```

## Quality checks

```powershell
ruff check app tests
ruff format --check app tests
pytest -q
```
