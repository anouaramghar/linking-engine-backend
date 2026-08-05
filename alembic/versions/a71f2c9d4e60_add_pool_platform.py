"""add pool platform

Revision ID: a71f2c9d4e60
Revises: e7b4c9d2a601
Create Date: 2026-07-31
"""

import sqlalchemy as sa
from alembic import op

revision = "a71f2c9d4e60"
down_revision = "e7b4c9d2a601"
branch_labels = None
depends_on = None

_OLD_PLATFORM = sa.Enum("wordpress", "html", name="platform", native_enum=False, length=20)
_NEW_PLATFORM = sa.Enum("wordpress", "html", "pool", name="platform", native_enum=False, length=20)


def upgrade() -> None:
    op.alter_column(
        "sites",
        "platform",
        existing_type=_OLD_PLATFORM,
        type_=_NEW_PLATFORM,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "sites",
        "platform",
        existing_type=_NEW_PLATFORM,
        type_=_OLD_PLATFORM,
        existing_nullable=False,
    )
