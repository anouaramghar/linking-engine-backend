"""add permanent content-pool approval traceability

Revision ID: c6f4a2d9e810
Revises: b5e1d8a3c740
Create Date: 2026-08-03 14:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "c6f4a2d9e810"
down_revision = "b5e1d8a3c740"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pool_source_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("site_name", sa.String(length=255), nullable=False),
        sa.Column("site_base_url", sa.String(length=2048), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("operator_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('approved', 'revoked', 'quarantined', 'reactivated')",
            name="ck_pool_source_audit_event_action",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pool_source_audit_events_site_created",
        "pool_source_audit_events",
        ["site_id", "created_at", "id"],
        unique=False,
    )

    # Preserve the latest legacy state as the first known event. Earlier actions
    # cannot be reconstructed honestly, so they are not invented.
    op.execute(
        """
        INSERT INTO pool_source_audit_events
            (site_id, site_name, site_base_url, action, operator_id, created_at)
        SELECT id, name, base_url, 'approved',
               COALESCE(pool_source_approved_by, 'legacy-unknown'),
               pool_source_approved_at
        FROM sites
        WHERE platform = 'pool'
          AND pool_source_approved = true
          AND pool_source_approved_at IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO pool_source_audit_events
            (site_id, site_name, site_base_url, action, operator_id, reason, created_at)
        SELECT id, name, base_url, 'quarantined', 'system',
               pool_source_quarantine_reason, pool_source_quarantined_at
        FROM sites
        WHERE platform = 'pool'
          AND pool_source_quarantined = true
          AND pool_source_quarantined_at IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO pool_source_audit_events
            (site_id, site_name, site_base_url, action, operator_id, created_at)
        SELECT id, name, base_url, 'reactivated',
               COALESCE(pool_source_last_reactivated_by, 'legacy-unknown'),
               pool_source_last_reactivated_at
        FROM sites
        WHERE platform = 'pool'
          AND pool_source_last_reactivated_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pool_source_audit_events_site_created",
        table_name="pool_source_audit_events",
    )
    op.drop_table("pool_source_audit_events")
