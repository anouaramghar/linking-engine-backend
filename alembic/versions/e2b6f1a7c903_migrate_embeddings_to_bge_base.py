"""migrate embeddings to BGE-base

Revision ID: e2b6f1a7c903
Revises: f18b6c4d2a90
Create Date: 2026-07-27 00:00:00.000000

"""

from alembic import op
from pgvector.sqlalchemy import Vector

revision = "e2b6f1a7c903"
down_revision = "f18b6c4d2a90"
branch_labels = None
depends_on = None

OLD_DIMENSION = 1024
NEW_DIMENSION = 768


def _replace_embedding_dimension(source: int, target: int) -> None:
    # Vectors from different embedding models are not convertible. Truncating is
    # both safer and substantially cheaper than rewriting every row through a cast;
    # the next per-site analysis rebuilds them from article content.
    op.execute("TRUNCATE TABLE embeddings RESTART IDENTITY")
    op.alter_column(
        "embeddings",
        "vector",
        existing_type=Vector(source),
        type_=Vector(target),
        existing_nullable=False,
        postgresql_using=f"vector::vector({target})",
    )


def upgrade() -> None:
    # Pending rankings were produced by the previous model. Expiring them preserves
    # history while removing them from the active cap and pair-exclusion rules, so
    # BGE-base can produce a clean replacement queue. Human-reviewed and applied
    # decisions deliberately remain intact.
    op.execute("UPDATE suggestions SET status = 'expired' WHERE status = 'pending'")
    _replace_embedding_dimension(OLD_DIMENSION, NEW_DIMENSION)


def downgrade() -> None:
    # A 768-dimensional BGE-base vector cannot be converted back to the former
    # 1024-dimensional contract. The old model must re-encode after downgrade too.
    # Expired suggestions are not reactivated because the migration cannot safely
    # distinguish them from suggestions expired for other reasons.
    _replace_embedding_dimension(NEW_DIMENSION, OLD_DIMENSION)
