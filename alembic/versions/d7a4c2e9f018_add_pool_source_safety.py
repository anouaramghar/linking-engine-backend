"""add content-pool approval and quarantine state

Revision ID: d7a4c2e9f018
Revises: a71f2c9d4e60
Create Date: 2026-08-01
"""

import sqlalchemy as sa
from alembic import op

revision = "d7a4c2e9f018"
down_revision = "a71f2c9d4e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column(
            "pool_source_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "sites",
        sa.Column("pool_source_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sites",
        sa.Column("pool_source_approved_by", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "sites",
        sa.Column(
            "pool_source_consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "sites",
        sa.Column(
            "pool_source_quarantined",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "sites",
        sa.Column("pool_source_quarantined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sites",
        sa.Column("pool_source_quarantine_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "sites",
        sa.Column("pool_source_last_reactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sites",
        sa.Column("pool_source_last_reactivated_by", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sites", "pool_source_last_reactivated_by")
    op.drop_column("sites", "pool_source_last_reactivated_at")
    op.drop_column("sites", "pool_source_quarantine_reason")
    op.drop_column("sites", "pool_source_quarantined_at")
    op.drop_column("sites", "pool_source_quarantined")
    op.drop_column("sites", "pool_source_consecutive_failures")
    op.drop_column("sites", "pool_source_approved_by")
    op.drop_column("sites", "pool_source_approved_at")
    op.drop_column("sites", "pool_source_approved")
