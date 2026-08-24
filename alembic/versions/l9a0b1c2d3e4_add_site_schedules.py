"""Add durable managed-site crawl and analysis schedules.

Revision ID: l9a0b1c2d3e4
Revises: k8b9c0d1e2f3
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op


revision = "l9a0b1c2d3e4"
down_revision = "k8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cadence", sa.String(length=16), nullable=False, server_default="daily"),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("local_time", sa.Time(), nullable=False, server_default="02:00:00"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_status", sa.String(length=16), nullable=True),
        sa.Column("last_attempt_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("site_id"),
        sa.CheckConstraint("cadence IN ('daily', 'weekly')", name="ck_site_schedules_cadence"),
        sa.CheckConstraint(
            "weekday IS NULL OR (weekday >= 0 AND weekday <= 6)",
            name="ck_site_schedules_weekday",
        ),
    )
    op.create_index(
        "ix_site_schedules_due",
        "site_schedules",
        ["enabled", "next_run_at"],
    )
    op.create_index("ix_site_schedules_site_id", "site_schedules", ["site_id"])
    op.add_column(
        "pipeline_batches",
        sa.Column(
            "schedule_id",
            sa.Integer(),
            sa.ForeignKey("site_schedules.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_pipeline_batches_schedule_id", "pipeline_batches", ["schedule_id"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_batches_schedule_id", table_name="pipeline_batches")
    op.drop_column("pipeline_batches", "schedule_id")
    op.drop_index("ix_site_schedules_site_id", table_name="site_schedules")
    op.drop_index("ix_site_schedules_due", table_name="site_schedules")
    op.drop_table("site_schedules")
