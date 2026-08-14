"""Shared fixtures, and the test-database isolation guard.

This suite talks to a real PostgreSQL on purpose: the suggestion pipeline is
mostly SQL — pgvector distance, the eligibility predicate, partial indexes — so
a stubbed engine would exercise almost none of it.

That makes pointing pytest at the wrong database a data-loss risk rather than an
inconvenience. Suggestion audit events explain changes but do not make a bulk
review reversible: a test that writes `approved` across a developer's real queue
still overwrites live workflow state. This has already happened once against the
`linkmesh` development database.

So the suite refuses to start unless it has been told, explicitly, which
database is disposable. Import order is load-bearing: the resolution below
rewrites ``DATABASE_URL`` in the environment *before* anything imports
``app.config``, because ``app.db`` builds its engine from ``settings`` at import
time and pydantic-settings lets a real environment variable win over ``.env``.
"""

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import SecretStr

#: Databases the suite must never write to, whatever the configuration says.
#: ``linkmesh`` is the development database; ``postgres`` is the cluster's
#: maintenance database.
PROTECTED_DATABASE_NAMES = frozenset({"linkmesh", "postgres", "template0", "template1"})

#: Without ``TEST_DATABASE_URL``, a database is accepted only when its name says
#: out loud that it is disposable.
TEST_DATABASE_PREFIX = "test_"
TEST_DATABASE_SUFFIX = "_test"

_SETUP_HELP = """
Create one and point the suite at it:

    docker compose up -d db
    docker compose exec db psql -U linkmesh -d postgres \\
        -c 'CREATE DATABASE linkmesh_test OWNER linkmesh'

    # bash / CI
    export TEST_DATABASE_URL='postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'
    # PowerShell
    $env:TEST_DATABASE_URL = 'postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test'

    # migrate it once (alembic reads DATABASE_URL)
    DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head

See README.md, "Running the tests".
"""


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _named_a_test_database(name: str) -> bool:
    return name.startswith(TEST_DATABASE_PREFIX) or name.endswith(TEST_DATABASE_SUFFIX)


def _resolve_test_database_url() -> str:
    """Return the URL the suite may write to, or refuse to run.

    ``TEST_DATABASE_URL`` is the intended path. A bare ``DATABASE_URL`` is
    accepted only when the database name itself declares the database
    disposable, so that configuring nothing fails loudly instead of quietly
    inheriting the developer's ``.env``.
    """
    explicit = os.environ.get("TEST_DATABASE_URL", "").strip()
    if explicit:
        name = _database_name(explicit)
        if not name:
            raise pytest.UsageError(
                f"TEST_DATABASE_URL names no database: {explicit!r}\n{_SETUP_HELP}"
            )
        if name in PROTECTED_DATABASE_NAMES:
            raise pytest.UsageError(
                f"TEST_DATABASE_URL points at the protected database {name!r}.\n"
                "The suite writes and deletes rows; it must never run against "
                f"development or maintenance data.\n{_SETUP_HELP}"
            )
        return explicit

    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured and _named_a_test_database(_database_name(configured)):
        return configured

    where = (
        f"DATABASE_URL={configured!r}"
        if configured
        else "neither TEST_DATABASE_URL nor DATABASE_URL is set, so app/config.py "
        "would fall back to .env or its development default"
    )
    raise pytest.UsageError(
        f"Refusing to run: no isolated test database is configured ({where}).\n"
        "The suite creates, updates, and deletes rows, and suggestion reviews are "
        "not recoverable once overwritten. Set TEST_DATABASE_URL, or name the "
        f"database {TEST_DATABASE_PREFIX}* / *{TEST_DATABASE_SUFFIX}.\n{_SETUP_HELP}"
    )


def _redacted(url: str) -> str:
    """The URL without its password, safe to print in a header or assertion."""
    parts = urlsplit(url)
    if parts.hostname is None:
        return url
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    netloc = f"{parts.username}:***@{host}" if parts.username else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


TEST_DATABASE_URL = _resolve_test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Only now is importing the application safe: app.db reads settings.database_url at
# import time, and the assignment above is what that read resolves to.
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, select  # noqa: E402

from app.api.deps import require_api_key  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Site, Tenant  # noqa: E402
from app.services import telegram  # noqa: E402
from app.services.authorization import Principal, ensure_default_tenant  # noqa: E402


@event.listens_for(Site, "before_insert")
def _assign_default_tenant(mapper, connection, target: Site) -> None:
    """Own tenant-less test sites under the default tenant.

    This lives in the suite, not in the model, on purpose. Dozens of tests
    predate tenancy and construct ``Site`` without an owner; backfilling them
    here keeps those tests readable. Registering it on the model instead would
    apply the same silent backfill to production inserts, turning "a code path
    forgot to set tenant_id" from a NOT NULL failure into a live site quietly
    filed under the tenant every admin can see.
    """
    if getattr(target, "tenant_id", None) is not None:
        return
    tenant_id = connection.scalar(select(Tenant.id).where(Tenant.slug == "default").limit(1))
    if tenant_id is None:
        result = connection.execute(
            Tenant.__table__.insert().values(slug="default", name="Default").returning(Tenant.id)
        )
        tenant_id = result.scalar_one()
    target.tenant_id = tenant_id


def pytest_report_header() -> str:
    return f"test database: {_redacted(TEST_DATABASE_URL)}"


@pytest.fixture(scope="session", autouse=True)
def engine_is_bound_to_the_test_database():
    """Second lock on the same door.

    The rewrite above happens at import time; this checks the engine that was
    actually built, so a stray ``create_engine`` or a re-imported settings object
    cannot quietly reintroduce the development database mid-session.
    """
    bound = engine.url.database
    assert bound not in PROTECTED_DATABASE_NAMES, (
        f"the SQLAlchemy engine is bound to the protected database {bound!r} "
        f"despite TEST_DATABASE_URL={_redacted(TEST_DATABASE_URL)}"
    )
    assert bound == _database_name(TEST_DATABASE_URL), (
        f"the SQLAlchemy engine is bound to {bound!r}, not the configured test "
        f"database {_database_name(TEST_DATABASE_URL)!r}"
    )
    yield


@pytest.fixture(autouse=True)
def disable_api_key(monkeypatch):
    # Keep unrelated endpoint tests focused on their own behavior while auth
    # tests remove this override and exercise the real dependency. Most ranking
    # tests use deliberately orthogonal toy vectors to isolate quotas and state
    # transitions; the dedicated score-floor test opts back into the production
    # threshold explicitly.
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(
        settings,
        "credential_encryption_key",
        SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="),
    )
    monkeypatch.setattr(settings, "credential_decryption_keys", None)
    monkeypatch.setattr(settings, "suggestion_min_score", 0.0)
    # Admin principal so route-level site ownership checks pass without a key.
    app.dependency_overrides[require_api_key] = lambda: Principal(
        is_admin=True, source="legacy_env"
    )
    yield
    app.dependency_overrides.pop(require_api_key, None)


@pytest.fixture(autouse=True)
def offline_publication_defaults(monkeypatch):
    """Two things a publication run does to the outside world that tests must not.

    The inter-article pause exists to be polite to a customer's host; against a
    mock transport it only adds real seconds. Preflight placement generation
    calls OpenRouter, which is a live third-party request and real money — with
    a key present in `.env` the suite silently started making them, which is
    both a cost and a source of four-minute test runs.

    Tests that exercise either one override the setting themselves.
    """
    monkeypatch.setattr(settings, "publish_request_delay_seconds", 0.0)
    monkeypatch.setattr(settings, "publish_max_placement_calls_per_run", 0)


@pytest.fixture(autouse=True)
def offline_tavily(monkeypatch):
    """Never let an unrelated test spend Tavily credits.

    Developer ``.env`` files may contain a live key.  Ranking tests that do not
    exercise the external-search provider must stay deterministic and offline;
    provider unit tests pass an explicit fake key and a mock HTTP transport.
    """
    monkeypatch.setattr(settings, "tavily_api_key", "")


@pytest.fixture(autouse=True)
def offline_telegram(monkeypatch):
    """Nothing in the suite may message Telegram for real.

    `.env` carries a live bot token and the login tests set a fake one, so an
    unpatched `notify` would either spam a real chat or post a bad token at
    api.telegram.org. Tests that care what was sent take this fixture by name
    and assert on the list.
    """
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        telegram, "notify", lambda telegram_id, text: sent.append((telegram_id, text))
    )
    return sent


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def site(db):
    """A throwaway site, cascade-deleted after the test.

    It carries a WordPress account, because a site without one cannot be
    prepared for publication at all and most callers here are exercising what
    happens *after* that gate. Only the username is set: the password is an
    encrypted column whose key is installed by an autouse fixture, and
    `has_wordpress_credentials` — the thing the gate reads — tests the username.
    """
    tenant = ensure_default_tenant(db)
    site = Site(
        name="test-site",
        base_url=f"https://test-{uuid.uuid4().hex[:8]}.example.com",
        platform="wordpress",
        wp_username="editor",
        tenant_id=tenant.id,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    yield site
    db.delete(site)
    db.commit()
