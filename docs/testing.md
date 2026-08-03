# Running the tests

The suite runs against a real PostgreSQL. Most of the suggestion pipeline is SQL
— pgvector distance, the eligibility predicate, the partial queue indexes — so a
stubbed engine would exercise almost none of the behaviour worth testing.

That has a consequence: **the tests write, update, and delete rows.** They must
never be pointed at a database anyone cares about.

## Why this is enforced rather than documented

Suggestions carry no history rows. A test that writes `approved` across the
queue destroys the previous `status` and `reviewed_at` values permanently —
there is nothing to restore from. This has already happened once: a bulk-review
test using hard-coded suggestion ids `1..1000` ran against the `linkmesh`
development database and moved nine real rows from `pending` to `approved`,
overwriting `reviewed_at` on every already-approved row in the process.

So `tests/conftest.py` refuses to start unless an isolated database is named
explicitly. There is no flag to skip the check.

## Setup

Create the database once:

```bash
docker compose up -d db
docker compose exec db psql -U linkmesh -d postgres \
    -c 'CREATE DATABASE linkmesh_test OWNER linkmesh'
```

Point the suite at it and migrate it:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
pytest -q
```

PowerShell:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'
$env:DATABASE_URL = $env:TEST_DATABASE_URL
alembic upgrade head
pytest -q
```

Re-run `alembic upgrade head` against the test database whenever a migration is
added; the suite does not create the schema for you.

Each run prints the database it resolved, with the password removed:

```
test database: postgresql+psycopg://linkmesh:***@127.0.0.1:15432/linkmesh_test
```

## What the guard accepts

| Configuration | Result |
| --- | --- |
| `TEST_DATABASE_URL` naming any non-protected database | runs against it |
| `DATABASE_URL` whose database is `test_*` or `*_test` | runs against it |
| `DATABASE_URL` naming anything else | **refused** |
| Neither set (falls back to `.env` or the development default) | **refused** |
| Either one naming `linkmesh`, `postgres`, `template0`, `template1` | **refused** |

`TEST_DATABASE_URL` is exported into the process as `DATABASE_URL` before
`app.config` is imported, because `app.db` builds its engine from `settings` at
import time. A session-scoped fixture then re-checks the engine that was
actually built, so a stray `create_engine` cannot reintroduce the development
database mid-session.

The guard's own behaviour is covered by `tests/test_database_isolation.py`.

## CI

Set `TEST_DATABASE_URL` in the job environment and run `alembic upgrade head`
against it before `pytest`. A CI database named `*_test` also satisfies the
guard through `DATABASE_URL` alone.
