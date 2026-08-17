"""default newly materialized external-link policies to disabled

Revision ID: f0a2b3c4d5e6
Revises: d1b7f4c2e809
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f0a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "d1b7f4c2e809"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Change only the default; preserve every existing policy value."""
    op.alter_column(
        "external_link_policies",
        "external_links_enabled",
        existing_type=sa.Boolean(),
        server_default=sa.false(),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Restore the historical default without rewriting persisted policies."""
    op.alter_column(
        "external_link_policies",
        "external_links_enabled",
        existing_type=sa.Boolean(),
        server_default=sa.true(),
        existing_nullable=False,
    )
