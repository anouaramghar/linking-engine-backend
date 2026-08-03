"""The guard that keeps this suite off the development database.

These assertions are about the guard's decision function rather than the running
session: by the time a test executes, resolution has already happened once at
conftest import. Calling `_resolve_test_database_url` directly with a patched
environment is what lets us prove the refusals without a second pytest process.
"""

import pytest

from tests.conftest import (
    PROTECTED_DATABASE_NAMES,
    TEST_DATABASE_URL,
    _database_name,
    _redacted,
    _resolve_test_database_url,
)
from app.db import engine

TEST_URL = "postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh_test"
DEV_URL = "postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/linkmesh"


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return monkeypatch


def test_the_session_is_bound_to_an_isolated_database():
    """The live engine — not a patched copy — must be off the protected list."""
    assert engine.url.database not in PROTECTED_DATABASE_NAMES
    assert engine.url.database == _database_name(TEST_DATABASE_URL)


def test_an_unconfigured_run_is_refused(clean_env):
    with pytest.raises(pytest.UsageError, match="no isolated test database is configured"):
        _resolve_test_database_url()


def test_a_developer_database_url_alone_is_refused(clean_env):
    """The failure mode that motivated the guard: inheriting the developer's .env."""
    clean_env.setenv("DATABASE_URL", DEV_URL)

    with pytest.raises(pytest.UsageError, match="no isolated test database is configured"):
        _resolve_test_database_url()


@pytest.mark.parametrize("protected", sorted(PROTECTED_DATABASE_NAMES))
def test_pointing_the_override_at_a_protected_database_is_refused(clean_env, protected):
    """Naming the database explicitly is not a way around the rule."""
    clean_env.setenv("TEST_DATABASE_URL", f"postgresql+psycopg://u:p@127.0.0.1:15432/{protected}")

    with pytest.raises(pytest.UsageError, match=f"protected database '{protected}'"):
        _resolve_test_database_url()


def test_an_override_without_a_database_name_is_refused(clean_env):
    clean_env.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:15432/")

    with pytest.raises(pytest.UsageError, match="names no database"):
        _resolve_test_database_url()


def test_the_explicit_override_wins_over_a_developer_database_url(clean_env):
    clean_env.setenv("DATABASE_URL", DEV_URL)
    clean_env.setenv("TEST_DATABASE_URL", TEST_URL)

    assert _resolve_test_database_url() == TEST_URL


@pytest.mark.parametrize("name", ["linkmesh_test", "test_linkmesh", "ci_test"])
def test_a_database_named_disposable_is_accepted_without_the_override(clean_env, name):
    url = f"postgresql+psycopg://linkmesh:linkmesh@127.0.0.1:15432/{name}"
    clean_env.setenv("DATABASE_URL", url)

    assert _resolve_test_database_url() == url


def test_the_reported_url_hides_the_password():
    """The header and assertion messages print this; it must not leak a secret."""
    redacted = _redacted("postgresql+psycopg://linkmesh:s3cret@127.0.0.1:15432/linkmesh_test")

    assert "s3cret" not in redacted
    assert redacted == "postgresql+psycopg://linkmesh:***@127.0.0.1:15432/linkmesh_test"
