"""Merge dashboard-auth/publication and Tavily migration histories.

Revision ID: c3e5f7a9b201
Revises: b4f1d2a7c903, d2f7c8a9b401
Create Date: 2026-08-11
"""

from collections.abc import Sequence


revision: str = "c3e5f7a9b201"
down_revision: str | Sequence[str] | None = ("b4f1d2a7c903", "d2f7c8a9b401")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
