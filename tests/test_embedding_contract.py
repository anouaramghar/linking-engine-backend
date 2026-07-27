import importlib.util
from pathlib import Path

from app.config import Settings
from app.models.article import EMBEDDING_DIM, Embedding

EXPECTED_MODEL = "BAAI/bge-base-en-v1.5"
EXPECTED_DIMENSION = 768
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "e2b6f1a7c903_migrate_embeddings_to_bge_base.py"
)


class RecordingOperations:
    def __init__(self):
        self.calls = []

    def execute(self, statement):
        self.calls.append(("execute", statement))

    def alter_column(self, table, column, **kwargs):
        self.calls.append(("alter_column", table, column, kwargs))


def _load_migration():
    spec = importlib.util.spec_from_file_location("bge_base_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_embedding_contract_is_bge_base(monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    defaults = Settings(_env_file=None)

    assert defaults.embedding_model == EXPECTED_MODEL
    assert EMBEDDING_DIM == EXPECTED_DIMENSION
    assert Embedding.__table__.c.vector.type.dim == EXPECTED_DIMENSION


def test_example_environment_uses_the_runtime_default():
    example = Path(".env.example").read_text()

    assert f"EMBEDDING_MODEL={EXPECTED_MODEL}" in example


def test_upgrade_expires_only_pending_suggestions_and_replaces_vectors(monkeypatch):
    migration = _load_migration()
    operations = RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert operations.calls[:2] == [
        ("execute", "UPDATE suggestions SET status = 'expired' WHERE status = 'pending'"),
        ("execute", "TRUNCATE TABLE embeddings RESTART IDENTITY"),
    ]
    operation, table, column, options = operations.calls[2]
    assert (operation, table, column) == ("alter_column", "embeddings", "vector")
    assert options["existing_type"].dim == 1024
    assert options["type_"].dim == EXPECTED_DIMENSION
    assert options["existing_nullable"] is False
    assert options["postgresql_using"] == "vector::vector(768)"


def test_downgrade_discards_incompatible_vectors_before_restoring_1024(monkeypatch):
    migration = _load_migration()
    operations = RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.calls[0] == ("execute", "TRUNCATE TABLE embeddings RESTART IDENTITY")
    operation, table, column, options = operations.calls[1]
    assert (operation, table, column) == ("alter_column", "embeddings", "vector")
    assert options["existing_type"].dim == EXPECTED_DIMENSION
    assert options["type_"].dim == 1024
    assert options["existing_nullable"] is False
    assert options["postgresql_using"] == "vector::vector(1024)"
