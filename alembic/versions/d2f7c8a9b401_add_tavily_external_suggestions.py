"""add direct external-search suggestions and audit events

Revision ID: d2f7c8a9b401
Revises: b8d2f4a6c701
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d2f7c8a9b401"
down_revision = "b8d2f4a6c701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "suggestions",
        "target_article_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column("suggestions", sa.Column("external_url", sa.String(length=2048)))
    op.add_column("suggestions", sa.Column("external_title", sa.Text()))
    op.add_column("suggestions", sa.Column("external_snippet", sa.Text()))
    op.add_column("suggestions", sa.Column("provider", sa.String(length=50)))
    op.add_column("suggestions", sa.Column("provider_request_id", sa.String(length=255)))
    op.add_column("suggestions", sa.Column("provider_score", sa.Float()))
    op.add_column("suggestions", sa.Column("search_query", sa.Text()))
    op.create_check_constraint(
        "ck_suggestions_exactly_one_target",
        "suggestions",
        "(target_article_id IS NOT NULL) <> (external_url IS NOT NULL)",
    )
    op.create_index(
        "uq_suggestions_active_source_external_url",
        "suggestions",
        ["source_article_id", "external_url"],
        unique=True,
        postgresql_where=sa.text("external_url IS NOT NULL AND status <> 'expired'"),
    )

    op.create_table(
        "external_search_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "suggestion_id",
            sa.Integer(),
            sa.ForeignKey("suggestions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "job_run_id",
            sa.Integer(),
            sa.ForeignKey("job_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255)),
        sa.Column("search_query", sa.Text(), nullable=False),
        sa.Column("candidate_url", sa.String(length=2048)),
        sa.Column("provider_score", sa.Float()),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    for column in (
        "site_id",
        "source_article_id",
        "suggestion_id",
        "job_run_id",
        "provider_request_id",
    ):
        op.create_index(
            f"ix_external_search_audit_events_{column}",
            "external_search_audit_events",
            [column],
        )
    op.create_index(
        "ix_external_search_audit_site_created",
        "external_search_audit_events",
        ["site_id", "created_at", "id"],
    )
    op.create_index(
        "ix_external_search_audit_source_created",
        "external_search_audit_events",
        ["source_article_id", "created_at", "id"],
    )


def _refuse_while_external_rows_exist() -> None:
    connection = op.get_bind()
    total = connection.scalar(
        sa.text("SELECT count(*) FROM suggestions WHERE external_url IS NOT NULL")
    )
    if total:
        raise RuntimeError(
            f"Refusing to downgrade: {total} direct external suggestion(s) exist. "
            "Export or remove them deliberately before making target_article_id mandatory."
        )


def downgrade() -> None:
    _refuse_while_external_rows_exist()
    # ``IF EXISTS`` also supports disposable databases that briefly ran the
    # pre-release form of this revision before audit events were added to it.
    # PostgreSQL drops the table-owned indexes together with the table.
    op.execute("DROP TABLE IF EXISTS external_search_audit_events")

    op.drop_index("uq_suggestions_active_source_external_url", table_name="suggestions")
    op.drop_constraint(
        "ck_suggestions_exactly_one_target",
        "suggestions",
        type_="check",
    )
    op.drop_column("suggestions", "search_query")
    op.drop_column("suggestions", "provider_score")
    op.drop_column("suggestions", "provider_request_id")
    op.drop_column("suggestions", "provider")
    op.drop_column("suggestions", "external_snippet")
    op.drop_column("suggestions", "external_title")
    op.drop_column("suggestions", "external_url")
    op.alter_column(
        "suggestions",
        "target_article_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
