"""add job_runs

Revision ID: 689fe839affd
Revises: 9511e0ed9499
Create Date: 2026-07-16 12:22:07.771942

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "689fe839affd"
down_revision = "9511e0ed9499"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ingestion",
                "analysis",
                "publication",
                name="job_kind",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="job_run_status",
                native_enum=False,
                length=20,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("queue_job_id", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_runs_site_id"), "job_runs", ["site_id"], unique=False)
    op.create_index(op.f("ix_job_runs_queue_job_id"), "job_runs", ["queue_job_id"], unique=False)
    op.create_index(
        "ix_job_runs_site_kind_status", "job_runs", ["site_id", "kind", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_job_runs_site_kind_status", table_name="job_runs")
    op.drop_index(op.f("ix_job_runs_queue_job_id"), table_name="job_runs")
    op.drop_index(op.f("ix_job_runs_site_id"), table_name="job_runs")
    op.drop_table("job_runs")
