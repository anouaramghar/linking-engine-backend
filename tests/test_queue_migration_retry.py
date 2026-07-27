from pathlib import Path
from runpy import run_path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    "revision",
    [
        "a7c2e91b5f34_index_the_paged_review_queue.py",
        "c4f8a2d91e73_index_the_default_active_queue.py",
    ],
)
def test_queue_migration_drops_an_invalid_same_named_index(monkeypatch, revision):
    migration = run_path(
        Path(__file__).parents[1] / "alembic" / "versions" / revision
    )
    operation = migration["op"]
    result = Mock()
    result.scalar_one_or_none.return_value = False
    bind = Mock()
    bind.execute.return_value = result
    execute = Mock()
    monkeypatch.setattr(operation, "get_bind", lambda: bind)
    monkeypatch.setattr(operation, "execute", execute)

    migration["_drop_invalid_index"]("ix_retry_probe")

    statement = str(execute.call_args.args[0])
    assert statement == 'DROP INDEX CONCURRENTLY IF EXISTS "ix_retry_probe"'
