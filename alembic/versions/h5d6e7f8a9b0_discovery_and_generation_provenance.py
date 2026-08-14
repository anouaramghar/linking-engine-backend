"""persist discovery diagnostics and suggestion generation provenance

Revision ID: h5d6e7f8a9b0
Revises: g4c5d6e7f8a9
Create Date: 2026-08-14
"""

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "h5d6e7f8a9b0"
down_revision = "g4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_runs",
        sa.Column("discovered_urls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("accepted_urls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("skipped_urls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column(
            "diagnostic_summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "ingestion_diagnostics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ingestion_run_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("reason_detail", sa.Text()),
        sa.Column("discovered_from", sa.Text()),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_status", sa.Integer()),
        sa.Column("content_type", sa.String(length=255)),
        sa.Column("final_url", sa.Text()),
        sa.Column("canonical_url", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_ingestion_diagnostics_site_id",
        "ingestion_diagnostics",
        ["site_id"],
    )
    op.create_index(
        "ix_ingestion_diagnostics_ingestion_run_id",
        "ingestion_diagnostics",
        ["ingestion_run_id"],
    )
    op.create_index(
        "ix_ingestion_diagnostics_run_state",
        "ingestion_diagnostics",
        ["ingestion_run_id", "state"],
    )
    op.create_index(
        "ix_ingestion_diagnostics_run_reason",
        "ingestion_diagnostics",
        ["ingestion_run_id", "reason_code"],
    )
    op.add_column(
        "suggestions",
        sa.Column("generation_job_run_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_suggestions_generation_job_run_id",
        "suggestions",
        ["generation_job_run_id"],
    )
    op.create_foreign_key(
        "fk_suggestions_generation_job_run_id",
        "suggestions",
        "job_runs",
        ["generation_job_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_suggestions_generation_job_run_id",
        "suggestions",
        type_="foreignkey",
    )
    op.drop_index("ix_suggestions_generation_job_run_id", table_name="suggestions")
    op.drop_column("suggestions", "generation_job_run_id")
    op.drop_index("ix_ingestion_diagnostics_run_reason", table_name="ingestion_diagnostics")
    op.drop_index("ix_ingestion_diagnostics_run_state", table_name="ingestion_diagnostics")
    op.drop_index("ix_ingestion_diagnostics_ingestion_run_id", table_name="ingestion_diagnostics")
    op.drop_index("ix_ingestion_diagnostics_site_id", table_name="ingestion_diagnostics")
    op.drop_table("ingestion_diagnostics")
    op.drop_column("ingestion_runs", "diagnostic_summary")
    op.drop_column("ingestion_runs", "skipped_urls")
    op.drop_column("ingestion_runs", "accepted_urls")
    op.drop_column("ingestion_runs", "discovered_urls")
