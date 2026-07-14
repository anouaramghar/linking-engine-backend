# LinkMesh Backend Container Stack Design

Date: 2026-07-13
Branch: `feat/v1-foundations`

## Context

The backend is a FastAPI linking engine backed by PostgreSQL with pgvector and Redis/RQ. It ingests WordPress or sitemap content, stores an internal-link graph, generates embedding-based suggestions, and publishes approved links through the relevant connector. The API enqueues ingestion, analysis, and publication jobs; an RQ worker executes those jobs.

The repository already defines PostgreSQL, Redis, API, and worker services in `docker-compose.yml`, but the tracked `Dockerfile` is empty and the ignored `.env` file is absent. The stack therefore cannot currently be built or started from a fresh checkout.

## Goals

- Build a reproducible Python 3.12 application image from the checked-in `uv.lock`.
- Include the full CPU ML dependency set so analysis jobs can create missing embeddings.
- Reuse one application image for the API, worker, and database migrations.
- Start the complete local stack in dependency order and apply Alembic migrations automatically.
- Preserve PostgreSQL, Redis, and Hugging Face model data across container recreation.
- Verify the running API, its database and Redis connections, migrations, and RQ worker readiness.

## Non-goals

- Containerizing the frontend. Its checked-out `main` branch contains only a README and no runnable application or container definition.
- Production secret management, TLS termination, horizontal scaling, or cloud deployment.
- Baking the `BAAI/bge-m3` model weights into the image.
- Changing application behavior, API contracts, database schema, or worker task logic.

## Chosen approach

Use one shared, full application image for the API, worker, and a transient migration service. This matches the existing Compose structure and avoids building the expensive ML dependency layer more than once. The API image will contain ML libraries it does not directly use; that is an accepted local-development trade-off in exchange for simplicity and consistency.

Split API and worker images are deferred until image size or deployment cadence becomes an operational concern.

## Application image

The Dockerfile will:

- Use a slim Python 3.12 Linux base.
- Pin `uv` to version `0.11.28`, which supports the repository's revision 3 lockfile.
- Install dependencies from `uv.lock` with frozen resolution, the `ml` extra, and no development dependency group.
- Separate dependency installation from source copying where practical so application edits reuse cached dependency layers.
- Put the project virtual environment on `PATH` so Compose commands can call `uvicorn`, `alembic`, and `rq` directly.
- Run the final application processes as a non-root user.
- Default to the FastAPI command `uvicorn app.main:app --host 0.0.0.0 --port 8000`; Compose overrides the command for migrations and the worker.

A `.dockerignore` file will exclude Git metadata, virtual environments, Python/test caches, local environment files, documentation used only for development, and other non-runtime artifacts. `pyproject.toml`, `uv.lock`, Alembic files, and application source remain in the build context.

## Compose services

### `db`

- Continue using `pgvector/pgvector:pg16`.
- Persist data in `pgdata`.
- Keep the existing `pg_isready` health check.
- Keep PostgreSQL on port 5432 inside the Compose network and expose it as host port 15432 because the development machine already runs PostgreSQL 18 on host port 5432.

### `redis`

- Continue using `redis:7-alpine`.
- Persist data in `redisdata`.
- Keep the existing `redis-cli ping` health check.
- Expose port 6379 for local development.

### `migrate`

- Use the shared application image.
- Run `alembic upgrade head` once.
- Start only after PostgreSQL is healthy.
- Exit successfully after the schema reaches the current revision.
- Prevent API and worker startup if migrations fail.

### `api`

- Use the shared application image and its default Uvicorn command.
- Start only after PostgreSQL and Redis are healthy and `migrate` completed successfully.
- Expose port 8000.
- Add a container health check against `http://127.0.0.1:8000/api/v1/health`. The endpoint already performs a database query and Redis ping, so it verifies all three runtime boundaries.

### `worker`

- Use the shared application image.
- Run `rq worker --url redis://redis:6379/0 default`.
- Start only after PostgreSQL and Redis are healthy and `migrate` completed successfully.
- Mount a persistent Hugging Face cache volume so the lazily downloaded embedding model survives container recreation.

## Configuration and secrets

The ignored `.env` file will be initialized locally from `.env.example`. Its host-facing `DATABASE_URL` uses `localhost:15432`, while Compose overrides `DATABASE_URL` and `REDIS_URL` with service-network addresses for all application-image services. The file otherwise supplies non-secret development defaults and empty external-search keys.

The `.env` file must not be copied into the image or committed. WordPress credentials and external API keys remain empty unless the developer explicitly supplies local values later.

## Runtime data flow

1. A client calls a FastAPI endpoint.
2. Synchronous API operations read or write PostgreSQL directly.
3. Ingestion, analysis, and publication endpoints enqueue RQ jobs in Redis.
4. The worker consumes jobs from the `default` queue.
5. Ingestion jobs crawl WordPress or sitemap content and update the article/link graph in PostgreSQL.
6. Analysis jobs lazily load `BAAI/bge-m3`, store missing 1024-dimensional embeddings through pgvector, and generate link suggestions.
7. Publication jobs apply approved suggestions through the site's connector and persist job outcomes.

The model is downloaded on the first analysis job rather than during image build. This keeps model lifecycle separate from dependency lifecycle while the persistent cache avoids repeated downloads.

## Failure handling

- Compose health conditions stop dependent services from starting against unavailable PostgreSQL or Redis instances.
- Migration failure blocks API and worker startup instead of allowing them to run against an incompatible schema.
- API health becomes unhealthy when the process, database, or Redis connection is unavailable.
- The worker's process state and RQ registration are checked separately because it has no HTTP endpoint.
- Existing named volumes are preserved. Verification must not use `docker compose down -v` or otherwise delete local data.
- Build and startup errors are investigated from the failing layer or service logs before changing configuration.

## Verification

Run the following checks in order:

1. `docker compose config` succeeds with the local `.env` present.
2. `docker compose build` builds the shared application image successfully.
3. The image can import `torch`, `torch_geometric`, and `sentence_transformers`.
4. `docker compose up -d` starts the stack.
5. The `migrate` service exits with status 0 and Alembic reports the head revision.
6. PostgreSQL, Redis, and API services report healthy.
7. `GET http://localhost:8000/api/v1/health` returns HTTP 200 with `database` and `redis` set to `up`.
8. The worker remains running and appears in RQ worker registration.
9. `docker compose ps` shows no unexpected exits or restarts.

## Acceptance criteria

- A fresh checkout with a local `.env` can build and start the backend stack with one Compose workflow.
- All long-running services are operational and the transient migration completes successfully.
- The worker has the full CPU ML dependency stack and a persistent model cache.
- The health endpoint proves API, PostgreSQL, and Redis connectivity.
- No source code, schema, or API behavior is changed beyond the container configuration required to run the existing project.
