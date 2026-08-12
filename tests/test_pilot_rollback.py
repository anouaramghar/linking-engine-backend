"""Rolling the pilot back must be safe, or refused before it changes anything.

These tests migrate a real database, so they build their own throwaway one per
test rather than reusing the suite's — a downgrade run against the shared test
database would drop a column out from under every other test.

The scratch database is created from a connection to the configured test
database, never from the `postgres` maintenance database, so the isolation guard
in `conftest` still describes everything this module touches.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import settings
from tests.conftest import TEST_DATABASE_URL, _database_name

PILOT_REVISION = "b8e5f1a3c027"
GLOBAL_REVISION = "c3d7a9f1e204"
CORRECTIVE_REVISION = "d4e6f8a1b203"
HYBRID_STANDARD_REVISION = "e7b4c9d2a601"
SITE_MODE_REVISION = "6a7d9e2c4b10"
#: The last revision that predates the pilot entirely.
PRE_PILOT_REVISION = "f3a8b1c2d4e5"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _scratch_url(name: str) -> str:
    return TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture
def scratch_database():
    """An empty database of its own, dropped however the test ends."""
    name = f"{_database_name(TEST_DATABASE_URL)}_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = _scratch_url(name)
    try:
        yield url
    finally:
        create_engine(url).dispose()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def migrated(scratch_database, monkeypatch):
    """The scratch database at head, with alembic pointed at it.

    `alembic/env.py` reads `settings.database_url` directly, so redirecting the
    setting is what redirects the migration.
    """
    monkeypatch.setattr(settings, "database_url", scratch_database)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "head")
    return config, scratch_database


def _seed(url: str, *, hybrid_statuses=(), cosine_statuses=(), site_mode="standard") -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        has_tenant_column = bool(
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'sites' AND column_name = 'tenant_id'"
                )
            ).scalar()
        )
        has_site_mode_column = bool(
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'sites' AND column_name = 'suggestion_mode'"
                )
            ).scalar()
        )
        if has_tenant_column and has_site_mode_column:
            # Seeded against the tenants-bearing head, sites must name an owner.
            site_stmt = text(
                "INSERT INTO sites (name, base_url, platform, suggestion_mode, tenant_id) "
                "SELECT 'rollback', 'https://rollback.example.com', 'wordpress', :mode, id "
                "FROM tenants WHERE slug = 'default' "
                "RETURNING id"
            )
        elif has_tenant_column:
            site_stmt = text(
                "INSERT INTO sites (name, base_url, platform, tenant_id) "
                "SELECT 'rollback', 'https://rollback.example.com', 'wordpress', id "
                "FROM tenants WHERE slug = 'default' "
                "RETURNING id"
            )
        elif has_site_mode_column:
            site_stmt = text(
                "INSERT INTO sites (name, base_url, platform, suggestion_mode) "
                "VALUES ('rollback', 'https://rollback.example.com', 'wordpress', :mode) "
                "RETURNING id"
            )
        else:
            site_stmt = text(
                "INSERT INTO sites (name, base_url, platform) "
                "VALUES ('rollback', 'https://rollback.example.com', 'wordpress') "
                "RETURNING id"
            )
        site_id = connection.execute(site_stmt, {"mode": site_mode}).scalar_one()
        article_ids = [
            connection.execute(
                text(
                    "INSERT INTO articles (site_id, url, title, content_text, is_active) "
                    "VALUES (:site_id, :url, :title, 'body', true) RETURNING id"
                ),
                {
                    "site_id": site_id,
                    "url": f"https://rollback.example.com/{index}",
                    "title": f"Article {index}",
                },
            ).scalar_one()
            for index in range(2 + len(hybrid_statuses) + len(cosine_statuses))
        ]
        source = article_ids[0]
        targets = iter(article_ids[1:])
        for method, statuses in (
            ("hybrid_bm25", hybrid_statuses),
            ("baseline_cosine", cosine_statuses),
        ):
            for status in statuses:
                connection.execute(
                    text(
                        "INSERT INTO suggestions "
                        "(site_id, source_article_id, target_article_id, method, score, "
                        " status, score_components) "
                        "VALUES (:site_id, :source, :target, :method, 0.8, :status, "
                        "        CAST(:components AS jsonb))"
                    ),
                    {
                        "site_id": site_id,
                        "source": source,
                        "target": next(targets),
                        "method": method,
                        "status": status,
                        "components": (
                            '{"version": "hybrid_bm25_v1", "bm25_score": 12.5}'
                            if method == "hybrid_bm25"
                            else None
                        ),
                    },
                )
    engine.dispose()


def _has_column(url: str, table: str, column: str) -> bool:
    engine = create_engine(url)
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    engine.dispose()
    return bool(found)


def _statuses(url: str) -> dict[str, int]:
    engine = create_engine(url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT status, count(*) FROM suggestions GROUP BY status")
        ).all()
    engine.dispose()
    return {status: count for status, count in rows}


def _site_modes(url: str) -> list[str]:
    engine = create_engine(url)
    with engine.connect() as connection:
        modes = list(
            connection.execute(text("SELECT suggestion_mode FROM sites ORDER BY id")).scalars()
        )
    engine.dispose()
    return modes


def test_the_pilot_migration_applies_and_reverts_on_a_clean_database(migrated):
    config, url = migrated
    assert _has_column(url, "suggestions", "score_components")

    command.downgrade(config, SITE_MODE_REVISION)

    assert not _has_column(url, "suggestions", "score_components")


def test_downgrade_is_refused_while_pending_and_reviewed_hybrid_rows_exist(migrated):
    """The required case: a queue holding both an unreviewed and a decided pilot row."""
    config, url = migrated
    _seed(url, hybrid_statuses=("pending", "approved", "applied", "rejected"))
    before = _statuses(url)

    with pytest.raises(RuntimeError) as failure:
        command.downgrade(config, SITE_MODE_REVISION)

    message = str(failure.value)
    assert "Refusing to downgrade" in message
    # The operator is told exactly what is in the way, by status.
    for status in ("pending=1", "approved=1", "applied=1", "rejected=1"):
        assert status in message
    assert "expire_pending_suggestions" in message

    # "Before changing anything": the column and every row survived the attempt.
    assert _has_column(url, "suggestions", "score_components")
    assert _statuses(url) == before


def test_downgrade_is_allowed_once_no_hybrid_rows_remain(migrated):
    config, url = migrated
    _seed(url, cosine_statuses=("pending", "approved"))

    command.downgrade(config, SITE_MODE_REVISION)

    assert not _has_column(url, "suggestions", "score_components")
    # Baseline rows are untouched by the rollback.
    assert _statuses(url) == {"pending": 1, "approved": 1}


def test_expiring_the_pending_rows_is_not_enough_while_decided_rows_remain(migrated):
    """Expiry frees the queue; it does not erase editorial history.

    A row an editor approved still records that the pilot chose it, so the
    rollback stays blocked until someone decides about those rows explicitly.
    """
    config, url = migrated
    _seed(url, hybrid_statuses=("pending", "approved"))
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE suggestions SET status = 'expired' WHERE status = 'pending'")
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="approved=1, expired=1"):
        command.downgrade(config, SITE_MODE_REVISION)


def test_hybrid_standard_migration_restores_experimental_for_existing_and_new_sites(migrated):
    """Databases already on the site-scoped revision have a safe forward path."""
    config, url = migrated
    command.downgrade(config, CORRECTIVE_REVISION)
    _seed(url, site_mode="standard")

    command.upgrade(config, HYBRID_STANDARD_REVISION)

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sites (name, base_url, platform) "
                "VALUES ('new hybrid', 'https://new-hybrid.example.com', 'html')"
            )
        )
    engine.dispose()
    assert _site_modes(url) == ["experimental", "experimental"]


def test_the_full_rollback_succeeds_from_the_corrected_site_scoped_head(migrated):
    config, url = migrated
    _seed(url, site_mode="experimental")

    command.downgrade(config, PRE_PILOT_REVISION)

    assert not _has_column(url, "suggestions", "score_components")
    assert not _has_column(url, "sites", "suggestion_mode")
