"""add tenants, api keys, and site ownership

Revision ID: b1c4e9a2f780
Revises: aac0c72b21a5
Create Date: 2026-08-05

Existing sites are assigned to a seeded default tenant so a single-operator
install keeps working under the legacy environment API_KEY (admin scope).
"""

from alembic import op
import sqlalchemy as sa


revision = "b1c4e9a2f780"
down_revision = "aac0c72b21a5"
branch_labels = None
depends_on = None

DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(is_admin = true AND tenant_id IS NULL) OR (is_admin = false AND tenant_id IS NOT NULL)",
            name="ck_api_keys_admin_xor_tenant",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    tenants = sa.table(
        "tenants",
        sa.column("id", sa.Integer),
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
    )
    op.execute(
        tenants.insert().values(slug=DEFAULT_TENANT_SLUG, name=DEFAULT_TENANT_NAME)
    )

    op.add_column("sites", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE sites SET tenant_id = (SELECT id FROM tenants WHERE slug = :slug)"
        ).bindparams(slug=DEFAULT_TENANT_SLUG)
    )
    op.alter_column("sites", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_sites_tenant_id_tenants",
        "sites",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_sites_tenant_id", "sites", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_sites_tenant_id", table_name="sites")
    op.drop_constraint("fk_sites_tenant_id_tenants", "sites", type_="foreignkey")
    op.drop_column("sites", "tenant_id")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("tenants")
