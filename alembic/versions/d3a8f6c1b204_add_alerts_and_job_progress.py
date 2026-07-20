"""add alerts and job progress

Revision ID: d3a8f6c1b204
Revises: b72c91e4a6f8
Create Date: 2026-07-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d3a8f6c1b204"
down_revision = "b72c91e4a6f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "occurrences",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alerts_dedupe",
        "alerts",
        ["site_id", "kind", "subject", "acknowledged_at"],
        unique=False,
    )
    op.create_index(
        "ix_alerts_site_last_seen_at",
        "alerts",
        ["site_id", "last_seen_at"],
        unique=False,
    )
    op.add_column(
        "job_runs",
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "job_runs",
        sa.Column("progress_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_runs", "progress_at")
    op.drop_column("job_runs", "progress")
    op.drop_index("ix_alerts_site_last_seen_at", table_name="alerts")
    op.drop_index("ix_alerts_dedupe", table_name="alerts")
    op.drop_table("alerts")
