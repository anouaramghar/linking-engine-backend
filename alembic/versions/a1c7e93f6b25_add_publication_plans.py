"""add immutable publication plans

Existing selected suggestions are deliberately not backfilled into approved
plans: nobody has seen the edit they produce, which is the whole point. They
migrate with publication_plan_id NULL, stay selected, and need preparation plus
an explicit human approval before anything can be published.

Revision ID: a1c7e93f6b25
Revises: e8a2c4f61d90
Create Date: 2026-08-10 12:20:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1c7e93f6b25"
down_revision = "e8a2c4f61d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("source_article_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "prepared",
                "approved",
                "applied",
                "stale",
                "superseded",
                "failed",
                name="publication_plan_status",
                native_enum=False,
                length=20,
            ),
            server_default="prepared",
            nullable=False,
        ),
        sa.Column("original_html", sa.Text(), nullable=False),
        sa.Column("updated_html", sa.Text(), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_hash", sa.String(length=64), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_publication_plans_site_id", "publication_plans", ["site_id"])
    op.create_index(
        "ix_publication_plans_source_article_id", "publication_plans", ["source_article_id"]
    )
    op.create_index("ix_publication_plans_plan_hash", "publication_plans", ["plan_hash"])
    op.create_index("ix_publication_plans_site_status", "publication_plans", ["site_id", "status"])
    # Two live snapshots of the same WordPress post would both look publishable
    # while only one can be correct. Terminal rows are excluded so the audit
    # history of one source article can grow without bound.
    op.create_index(
        "ux_publication_plans_active_source",
        "publication_plans",
        ["source_article_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('prepared', 'approved')"),
    )

    op.add_column("suggestions", sa.Column("publication_plan_id", sa.Integer(), nullable=True))
    op.create_index("ix_suggestions_publication_plan_id", "suggestions", ["publication_plan_id"])
    op.create_foreign_key(
        "fk_suggestions_publication_plan_id",
        "suggestions",
        "publication_plans",
        ["publication_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_suggestions_publication_plan_id", "suggestions", type_="foreignkey")
    op.drop_index("ix_suggestions_publication_plan_id", table_name="suggestions")
    op.drop_column("suggestions", "publication_plan_id")

    op.drop_index("ux_publication_plans_active_source", table_name="publication_plans")
    op.drop_index("ix_publication_plans_site_status", table_name="publication_plans")
    op.drop_index("ix_publication_plans_plan_hash", table_name="publication_plans")
    op.drop_index("ix_publication_plans_source_article_id", table_name="publication_plans")
    op.drop_index("ix_publication_plans_site_id", table_name="publication_plans")
    op.drop_table("publication_plans")
