"""add evaluation snapshots

Revision ID: b2d4e6f8a010
Revises: f9a1c3e5b702
Create Date: 2026-08-05
"""

import sqlalchemy as sa

from alembic import op

revision = "b2d4e6f8a010"
down_revision = "f9a1c3e5b702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("active_articles", sa.Integer(), nullable=False),
        sa.Column("orphan_pages", sa.Integer(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("snapshot_date", "site_id"),
    )
    op.create_index(
        "ix_evaluation_snapshots_snapshot_date",
        "evaluation_snapshots",
        ["snapshot_date"],
    )
    op.create_index(
        "ix_evaluation_snapshots_site_id",
        "evaluation_snapshots",
        ["site_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_snapshots_site_id", table_name="evaluation_snapshots")
    op.drop_index("ix_evaluation_snapshots_snapshot_date", table_name="evaluation_snapshots")
    op.drop_table("evaluation_snapshots")
