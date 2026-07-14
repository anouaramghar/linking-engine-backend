# LinkMesh Backend Container Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the LinkMesh backend's PostgreSQL, Redis, migration, FastAPI, and full-ML RQ worker services from a reproducible shared application image.

**Architecture:** A single Python 3.12 image, locked with `uv` and the `ml` extra, is shared by the API, migration, and worker services. Compose health gates and a one-shot Alembic service enforce startup order; named volumes persist database, queue, and Hugging Face model data.

**Tech Stack:** Docker Desktop, Docker Compose v5, Python 3.12, uv 0.11.28, FastAPI/Uvicorn, PostgreSQL 16 + pgvector, Redis 7, RQ, Alembic, sentence-transformers, PyTorch, PyTorch Geometric.

## Global Constraints

- Work only in `linking-engine-backend` on `feat/v1-foundations`.
- Use Python `3.12` and pin `uv` to `0.11.28`.
- Resolve dependencies from `uv.lock` with `--frozen --no-dev --extra ml`.
- Use one `linkmesh-engine:local` image for API, migrations, and worker.
- Keep PostgreSQL on `db:5432` internally and publish it as `localhost:15432`; host port 5432 belongs to an existing PostgreSQL 18 installation.
- Run application processes as a non-root `linkmesh` user.
- Keep `.env` ignored and out of the image; never commit secrets.
- Preserve `pgdata`, `redisdata`, and `hf_cache`; never run `docker compose down -v`.
- Leave the verified stack running when implementation finishes.
- Do not change application source, API contracts, schema, task logic, or the frontend repository.

## File map

- Modify `Dockerfile`: define the shared Python application image and default API process.
- Create `.dockerignore`: keep secrets, local state, and development-only files out of the image context.
- Modify `docker-compose.yml`: add the shared image contract, migration service, dependency gates, API health check, and model cache.
- Create `.env` locally: provide ignored development defaults required by Compose; this file is never staged.
- Preserve `docs/superpowers/specs/2026-07-13-container-stack-design.md`: approved design and acceptance criteria.

---

### Task 1: Build the shared full-ML application image

**Files:**
- Modify: `Dockerfile`
- Create: `.dockerignore`
- Test: Docker build and runtime import commands

**Interfaces:**
- Consumes: `pyproject.toml`, `uv.lock`, `app/`, `alembic.ini`, and `alembic/`.
- Produces: image `linkmesh-engine:local` with `uvicorn`, `alembic`, `rq`, `torch`, `torch_geometric`, and `sentence_transformers` on `PATH`.

- [ ] **Step 1: Run the failing image build**

Run:

```powershell
docker build --progress=plain --tag linkmesh-engine:local .
```

Expected: FAIL because the tracked `Dockerfile` is empty; no `linkmesh-engine:local` image is produced from the application.

- [ ] **Step 2: Implement the minimal shared image**

Replace `Dockerfile` with:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/home/linkmesh/.cache/huggingface

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ml --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ml

RUN groupadd --system linkmesh \
    && useradd --system --gid linkmesh --home-dir /home/linkmesh --create-home linkmesh \
    && mkdir -p /home/linkmesh/.cache/huggingface \
    && chown -R linkmesh:linkmesh /app /opt/venv /home/linkmesh

USER linkmesh

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Create `.dockerignore` with:

```dockerignore
.git
.gitignore
.env
.env.*
!.env.example
.venv
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
*.egg-info/
tests/
docs/
.vscode/
.idea/
Thumbs.db
.DS_Store
README.md
```

- [ ] **Step 3: Build the image and verify the original failure is resolved**

Run:

```powershell
docker build --progress=plain --tag linkmesh-engine:local .
```

Expected: exit 0 and final output naming `linkmesh-engine:local`. The initial build may be large because it installs the CPU ML stack.

- [ ] **Step 4: Verify runtime dependencies and non-root execution**

Run:

```powershell
docker run --rm linkmesh-engine:local python -c "import os, torch, torch_geometric, sentence_transformers; assert os.geteuid() != 0; print(torch.__version__, torch_geometric.__version__, sentence_transformers.__version__)"
```

Expected: exit 0 and three package versions. The assertion proves the default image user is not root.

- [ ] **Step 5: Commit the image definition**

```powershell
git add -- Dockerfile .dockerignore
git commit -m "build: add full ML application image"
```

Expected: one commit containing only `Dockerfile` and `.dockerignore`.

---

### Task 2: Define dependency-gated Compose orchestration

**Files:**
- Modify: `docker-compose.yml`
- Create locally, never stage: `.env`
- Test: rendered Compose JSON assertions

**Interfaces:**
- Consumes: image `linkmesh-engine:local` from Task 1 and existing database/Redis service contracts.
- Produces: services `db`, `redis`, `migrate`, `api`, and `worker`; volumes `pgdata`, `redisdata`, and `hf_cache`.

- [ ] **Step 1: Create the ignored local environment file**

Create `.env` with:

```dotenv
DATABASE_URL=postgresql+psycopg://linkmesh:linkmesh@localhost:15432/linkmesh
REDIS_URL=redis://localhost:6379/0
API_HOST=0.0.0.0
API_PORT=8000
ENVIRONMENT=development
BRAVE_API_KEY=
TAVILY_API_KEY=
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DEVICE=cpu
```

Verify it is ignored:

```powershell
git status --short --ignored -- .env
```

Expected: `!! .env`.

- [ ] **Step 2: Run a failing orchestration assertion**

Run:

```powershell
$config = (docker compose config --format json) | ConvertFrom-Json
$required = @('db', 'redis', 'migrate', 'api', 'worker')
$actual = @($config.services.PSObject.Properties.Name)
$missing = @($required | Where-Object { $_ -notin $actual })
if ($missing.Count -gt 0) { throw "Missing services: $($missing -join ', ')" }
if ($config.services.api.image -ne 'linkmesh-engine:local') { throw 'API does not use shared image' }
if (-not $config.services.api.healthcheck) { throw 'API health check is missing' }
```

Expected: FAIL with `Missing services: migrate` against the original Compose file.

- [ ] **Step 3: Implement the Compose service graph**

Replace `docker-compose.yml` with:

```yaml
x-app-service: &app-service
  image: linkmesh-engine:local
  env_file:
    - .env
  environment:
    DATABASE_URL: postgresql+psycopg://linkmesh:linkmesh@db:5432/linkmesh
    REDIS_URL: redis://redis:6379/0

services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: linkmesh
      POSTGRES_PASSWORD: linkmesh
      POSTGRES_DB: linkmesh
    ports:
      - "15432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U linkmesh"]
      interval: 5s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 10

  migrate:
    <<: *app-service
    command: ["alembic", "upgrade", "head"]
    depends_on:
      db:
        condition: service_healthy
    restart: "no"

  api:
    <<: *app-service
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5)"
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 10s

  worker:
    <<: *app-service
    command: ["rq", "worker", "--url", "redis://redis:6379/0", "default"]
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    volumes:
      - hf_cache:/home/linkmesh/.cache/huggingface

volumes:
  pgdata:
  redisdata:
  hf_cache:
```

- [ ] **Step 4: Re-run the orchestration assertion**

Run the exact PowerShell assertion from Step 2 again.

Expected: exit 0 with no exception.

- [ ] **Step 5: Validate the fully rendered Compose configuration**

Run:

```powershell
docker compose config --quiet
docker compose config --services
docker compose config --volumes
docker compose config --images | Sort-Object -Unique
```

Expected services: `db`, `redis`, `migrate`, `api`, `worker`. Expected volumes: `pgdata`, `redisdata`, `hf_cache`. Expected images include `linkmesh-engine:local`, `pgvector/pgvector:pg16`, and `redis:7-alpine`.

- [ ] **Step 6: Commit the orchestration definition**

```powershell
git add -- docker-compose.yml
git commit -m "build: orchestrate backend container stack"
```

Expected: the ignored `.env` is not included in the commit.

---

### Task 3: Start and verify the complete stack

**Files:**
- Verify only; no tracked file changes

**Interfaces:**
- Consumes: the shared image and service graph from Tasks 1 and 2.
- Produces: a running API, database, Redis instance, and RQ worker plus a successfully completed migration container.

- [ ] **Step 1: Confirm required host ports are available**

Run:

```powershell
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in 15432, 6379, 8000 } |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

Expected: no unrelated listener owns ports 15432, 6379, or 8000. If a listener exists, identify it before proceeding; do not stop unrelated processes or containers.

- [ ] **Step 2: Build through Compose and start the stack**

Run:

```powershell
docker compose build api
docker compose up -d
```

Expected: image build exits 0; Compose creates the three named volumes, migration container, and four long-running services.

- [ ] **Step 3: Wait on actual container conditions**

Run:

```powershell
function Get-LinkMeshContainerState([string]$service) {
  $id = docker compose ps --all --quiet $service
  if (-not $id) { return 'missing|none|1' }
  docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.ExitCode}}' $id
}

$deadline = (Get-Date).AddMinutes(5)
do {
  $db = Get-LinkMeshContainerState 'db'
  $redis = Get-LinkMeshContainerState 'redis'
  $migrate = Get-LinkMeshContainerState 'migrate'
  $api = Get-LinkMeshContainerState 'api'
  $worker = Get-LinkMeshContainerState 'worker'

  if ($migrate -match '^exited\|none\|[1-9]') {
    docker compose logs migrate
    throw "Migration failed: $migrate"
  }

  $ready = (
    $db -eq 'running|healthy|0' -and
    $redis -eq 'running|healthy|0' -and
    $migrate -eq 'exited|none|0' -and
    $api -eq 'running|healthy|0' -and
    $worker -eq 'running|none|0'
  )

  if (-not $ready) { Start-Sleep -Seconds 2 }
} while (-not $ready -and (Get-Date) -lt $deadline)

if (-not $ready) {
  docker compose ps --all
  docker compose logs --tail 100
  throw "Stack did not become ready. db=$db redis=$redis migrate=$migrate api=$api worker=$worker"
}

Write-Output "db=$db redis=$redis migrate=$migrate api=$api worker=$worker"
```

Expected: `db=running|healthy|0 redis=running|healthy|0 migrate=exited|none|0 api=running|healthy|0 worker=running|none|0`.

- [ ] **Step 4: Verify migrations and API health**

Run:

```powershell
docker compose logs migrate
docker compose exec -T api alembic current
$health = Invoke-RestMethod -Uri 'http://localhost:8000/api/v1/health'
$health | ConvertTo-Json -Compress
```

Expected: migration exits without error; Alembic reports `9511e0ed9499 (head)`; health JSON is `{"status":"ok","database":"up","redis":"up"}`.

- [ ] **Step 5: Verify worker registration and final service state**

Run:

```powershell
docker compose exec -T worker rq info --url redis://redis:6379/0
docker compose ps --all
```

Expected: RQ reports one active worker listening to `default`; database, Redis, API, and worker are running, API/database/Redis are healthy, and migration is exited with code 0.

---

### Task 4: Run regression checks and preserve the running stack

**Files:**
- Verify only; `.venv` remains ignored

**Interfaces:**
- Consumes: the healthy stack from Task 3.
- Produces: evidence that existing backend integration tests and repository hygiene remain intact.

- [ ] **Step 1: Install the locked development test group locally**

Run:

```powershell
uv sync --frozen --group dev
```

Expected: exit 0 and an ignored `.venv` containing pytest and the application dependencies.

- [ ] **Step 2: Run the existing backend test suite against the containerized dependencies**

Run:

```powershell
uv run --frozen --no-sync pytest -q
```

Expected: all tests pass with zero failures while PostgreSQL and Redis remain provided by Compose.

- [ ] **Step 3: Run final container and repository checks**

Run:

```powershell
docker compose ps --all
docker compose exec -T worker rq info --url redis://redis:6379/0
Invoke-RestMethod -Uri 'http://localhost:8000/api/v1/health' | ConvertTo-Json -Compress
git diff --check
git status --short --branch
```

Expected: healthy running services, completed migration, one registered worker, healthy API JSON, no whitespace errors, and no uncommitted tracked changes. `.env` and `.venv` remain ignored. Do not stop the stack.
