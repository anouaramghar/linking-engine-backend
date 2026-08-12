"""add per-site editorial feedback policy

Revision ID: a7c9e1f3b506
Revises: e5c7a9d1b304
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c9e1f3b506"
down_revision = "e5c7a9d1b304"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_pipeline_batch_status", "pipeline_batches", type_="check")
    op.create_check_constraint(
        "ck_pipeline_batch_status",
        "pipeline_batches",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'partial_failed', 'cancelled')",
    )
    op.drop_constraint("ck_pipeline_site_status", "pipeline_site_runs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_site_status",
        "pipeline_site_runs",
        "status IN ('queued', 'ingestion_running', 'analysis_queued', "
        "'analysis_running', 'succeeded', 'failed', 'cancelled')",
    )
    op.add_column(
        "sites",
        sa.Column("editorial_feedback_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "sites",
        sa.Column("editorial_min_score_percent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sites",
        sa.Column("editorial_feedback_weight", sa.Float(), nullable=False, server_default="0.20"),
    )
    op.add_column(
        "sites",
        sa.Column("editorial_feedback_min_samples", sa.Integer(), nullable=False, server_default="10"),
    )
    op.create_check_constraint(
        "ck_sites_editorial_min_score_percent",
        "sites",
        "editorial_min_score_percent BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_sites_editorial_feedback_weight",
        "sites",
        "editorial_feedback_weight BETWEEN 0 AND 1",
    )
    op.create_check_constraint(
        "ck_sites_editorial_feedback_min_samples",
        "sites",
        "editorial_feedback_min_samples BETWEEN 1 AND 10000",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sites_editorial_feedback_min_samples", "sites", type_="check")
    op.drop_constraint("ck_sites_editorial_feedback_weight", "sites", type_="check")
    op.drop_constraint("ck_sites_editorial_min_score_percent", "sites", type_="check")
    op.drop_column("sites", "editorial_feedback_min_samples")
    op.drop_column("sites", "editorial_feedback_weight")
    op.drop_column("sites", "editorial_min_score_percent")
    op.drop_column("sites", "editorial_feedback_enabled")
    op.execute("UPDATE pipeline_site_runs SET status = 'failed' WHERE status = 'cancelled'")
    op.execute("UPDATE pipeline_batches SET status = 'failed' WHERE status = 'cancelled'")
    op.drop_constraint("ck_pipeline_site_status", "pipeline_site_runs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_site_status",
        "pipeline_site_runs",
        "status IN ('queued', 'ingestion_running', 'analysis_queued', "
        "'analysis_running', 'succeeded', 'failed')",
    )
    op.drop_constraint("ck_pipeline_batch_status", "pipeline_batches", type_="check")
    op.create_check_constraint(
        "ck_pipeline_batch_status",
        "pipeline_batches",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'partial_failed')",
    )
