"""add per-site external-link safety policies

Revision ID: e5c7a9d1b304
Revises: b2d4e6f8a010
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5c7a9d1b304"
down_revision = "b2d4e6f8a010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("domain_registered_at", sa.Date(), nullable=True))
    op.create_table(
        "external_link_policies",
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "external_links_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("require_https", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_trust_score", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("min_domain_age_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "trusted_tlds",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allowlist_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "blocklist_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "competitor_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "min_trust_score >= 0 AND min_trust_score <= 100",
            name="ck_external_link_policy_trust_score",
        ),
        sa.CheckConstraint(
            "min_domain_age_days >= 0",
            name="ck_external_link_policy_domain_age",
        ),
    )


def downgrade() -> None:
    op.drop_table("external_link_policies")
    op.drop_column("sites", "domain_registered_at")
