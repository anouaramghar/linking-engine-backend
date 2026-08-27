"""Record schedule editors for guarded agent updates.

Revision ID: m0b1c2d3e4f5
Revises: l9a0b1c2d3e4
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op


revision = "m0b1c2d3e4f5"
down_revision = "l9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("site_schedules", sa.Column("created_by", sa.String(length=255), nullable=True))
    op.add_column("site_schedules", sa.Column("updated_by", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("site_schedules", "updated_by")
    op.drop_column("site_schedules", "created_by")
