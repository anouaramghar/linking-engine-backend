"""Add one-time human-confirmed MCP action receipts.

Revision ID: k8b9c0d1e2f3
Revises: j7a8b9c0d1e2
Create Date: 2026-08-23
"""

import sqlalchemy as sa
from alembic import op

revision = "k8b9c0d1e2f3"
down_revision = "j7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_action_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("principal_binding", sa.JSON(), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("action_kind", sa.String(64), nullable=False),
        sa.Column("requires_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "confirmed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("dashboard_users.id", ondelete="SET NULL"),
        ),
        sa.Column("confirmed_by_telegram_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_status", sa.String(16), nullable=False, server_default="issued"),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_result", sa.JSON()),
        sa.Column("execution_error", sa.Text()),
    )
    op.create_index(
        "ix_agent_action_receipts_receipt_hash",
        "agent_action_receipts",
        ["receipt_hash"],
        unique=True,
    )
    for column in (
        "proposal_hash",
        "action_kind",
        "confirmed_by_user_id",
        "expires_at",
        "consumed_at",
        "execution_status",
    ):
        op.create_index(f"ix_agent_action_receipts_{column}", "agent_action_receipts", [column])


def downgrade() -> None:
    op.drop_table("agent_action_receipts")
