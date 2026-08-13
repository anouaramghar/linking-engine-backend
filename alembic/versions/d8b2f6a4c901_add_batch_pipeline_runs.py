"""add durable batch pipeline runs

Revision ID: d8b2f6a4c901
Revises: c6f4a2d9e810
Create Date: 2026-08-04 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "d8b2f6a4c901"
down_revision = "c6f4a2d9e810"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'partial_failed')",
            name="ck_pipeline_batch_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pipeline_site_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(length=20), server_default="ingestion", nullable=False),
        sa.Column("ingestion_job_run_id", sa.Integer(), nullable=True),
        sa.Column("analysis_job_run_id", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'ingestion_running', 'analysis_queued', "
            "'analysis_running', 'succeeded', 'failed')",
            name="ck_pipeline_site_status",
        ),
        sa.CheckConstraint(
            "stage IN ('ingestion', 'analysis', 'completed')", name="ck_pipeline_site_stage"
        ),
        sa.ForeignKeyConstraint(["analysis_job_run_id"], ["job_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["batch_id"], ["pipeline_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingestion_job_run_id"], ["job_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "site_id"),
    )
    op.create_index(
        "ix_pipeline_site_runs_batch_id", "pipeline_site_runs", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_pipeline_site_runs_batch_status",
        "pipeline_site_runs",
        ["batch_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_site_runs_site_id", "pipeline_site_runs", ["site_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_site_runs_site_id", table_name="pipeline_site_runs")
    op.drop_index("ix_pipeline_site_runs_batch_status", table_name="pipeline_site_runs")
    op.drop_index("ix_pipeline_site_runs_batch_id", table_name="pipeline_site_runs")
    op.drop_table("pipeline_site_runs")
    op.drop_table("pipeline_batches")
