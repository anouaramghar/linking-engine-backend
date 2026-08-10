# LinkMesh backend

The LinkMesh engine crawls connected sites, generates internal-link suggestions,
supports operator review, and publishes approved links. FastAPI serves the API;
PostgreSQL stores durable state; Redis and RQ run ingestion, analysis, and publication
jobs.

## Local stack

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
