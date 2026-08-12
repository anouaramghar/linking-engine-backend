"""The publication-review migration must be reversible with real rows present.

`b4f1d2a7c903` widens `job_runs.kind` from 20 to 32 characters so it can hold
'publication_preparation'. The downgrade used to narrow it straight back, which
PostgreSQL refuses the moment one such row exists — and those rows are the
operational record of who asked for a preparation and what it produced.

Like `test_pilot_rollback`, every test here migrates a throwaway database of its
own. A downgrade run against the shared test database would pull a column out
from under every other test in the suite.
"""

from alembic import command
from sqlalchemy import create_engine, text

from tests import test_pilot_rollback

# The scratch-database machinery carries the same isolation guarantee, so it is
# reused rather than rebuilt. Rebinding the fixtures registers them in this
# module, which is how pytest finds them.
scratch_database = test_pilot_rollback.scratch_database
migrated = test_pilot_rollback.migrated

#: The revision that widened `job_runs.kind` and added `requested_by`.
PUBLICATION_REVISION = "b4f1d2a7c903"
#: The revision immediately before it.
PRE_PUBLICATION_REVISION = "a1c7e93f6b25"

PREPARATION_KIND = "publication_preparation"


def _seed_preparation_job(url: str) -> int:
    """One durable preparation job, as the async prepare endpoint records it."""
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            site_id = connection.execute(
                text(
                    "INSERT INTO sites (name, base_url, platform, tenant_id) "
                    "SELECT 'migration', 'https://migration.example.com', 'wordpress', id "
                    "FROM tenants WHERE slug = 'default' "
                    "RETURNING id"
                )
            ).scalar_one()
            return connection.execute(
                text(
                    "INSERT INTO job_runs "
                    "(site_id, kind, status, queue_job_id, requested_by, attempts) "
                    "VALUES (:site_id, :kind, 'succeeded', 'prepare-1', "
                    "        'telegram:4242', 1) "
                    "RETURNING id"
                ),
                {"site_id": site_id, "kind": PREPARATION_KIND},
            ).scalar_one()
    finally:
        engine.dispose()


def _job_kind(url: str, job_id: int) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT kind FROM job_runs WHERE id = :id"), {"id": job_id}
            ).scalar()
    finally:
        engine.dispose()


def _kind_width(url: str) -> int | None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = 'job_runs' AND column_name = 'kind'"
                )
            ).scalar()
    finally:
        engine.dispose()


def _has_column(url: str, table: str, column: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :table AND column_name = :column"
                    ),
                    {"table": table, "column": column},
                ).scalar()
            )
    finally:
        engine.dispose()


def test_the_downgrade_survives_a_real_preparation_job_row(migrated):
    """Upgrade, insert, downgrade, upgrade — with the evidence row still there."""
    config, url = migrated
    job_id = _seed_preparation_job(url)
    assert _job_kind(url, job_id) == PREPARATION_KIND

    command.downgrade(config, PRE_PUBLICATION_REVISION)

    # The row is untouched: not deleted, and not relabelled 'publication' to fit
    # a narrower column. Its kind is what it was, so the audit trail still says
    # which job prepared which batch.
    assert _job_kind(url, job_id) == PREPARATION_KIND
    # What the downgrade *does* undo.
    assert not _has_column(url, "job_runs", "requested_by")

    command.upgrade(config, "head")

    assert _job_kind(url, job_id) == PREPARATION_KIND
    assert _has_column(url, "job_runs", "requested_by")


def test_the_kind_width_is_expand_only(migrated):
    """Older application code writes kinds of at most 20 characters.

    A wider VARCHAR accepts every one of them, so keeping the width is the
    cheaper half of the trade against refusing the downgrade outright.
    """
    config, url = migrated
    _seed_preparation_job(url)
    assert _kind_width(url) == 32

    command.downgrade(config, PRE_PUBLICATION_REVISION)

    assert _kind_width(url) == 32
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            site_id = connection.execute(text("SELECT id FROM sites LIMIT 1")).scalar_one()
            # The kinds the pre-publication code knows still insert cleanly.
            for kind in ("ingestion", "analysis", "publication"):
                connection.execute(
                    text(
                        "INSERT INTO job_runs (site_id, kind, status) "
                        "VALUES (:site_id, :kind, 'queued')"
                    ),
                    {"site_id": site_id, "kind": kind},
                )
    finally:
        engine.dispose()


def test_the_round_trip_ends_on_one_alembic_head(migrated):
    config, url = migrated
    _seed_preparation_job(url)
    command.downgrade(config, PRE_PUBLICATION_REVISION)

    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            heads = list(connection.execute(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()
    assert len(heads) == 1
