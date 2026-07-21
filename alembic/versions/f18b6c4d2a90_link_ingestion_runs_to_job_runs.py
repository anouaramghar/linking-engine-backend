"""link ingestion runs to durable job runs

Revision ID: f18b6c4d2a90
Revises: d3a8f6c1b204
Create Date: 2026-07-20 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f18b6c4d2a90"
down_revision = "d3a8f6c1b204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_runs",
        sa.Column("job_run_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ingestion_runs_job_run_id_job_runs",
        "ingestion_runs",
        "job_runs",
        ["job_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ingestion_runs_job_run_id",
        "ingestion_runs",
        ["job_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_runs_job_run_id", table_name="ingestion_runs")
    op.drop_constraint(
        "fk_ingestion_runs_job_run_id_job_runs",
        "ingestion_runs",
        type_="foreignkey",
    )
    op.drop_column("ingestion_runs", "job_run_id")
