"""add dashboard users, login nonces and sessions

Operator identity for the dashboard, so the UI stops being an unauthenticated
front door holding the shared admin key. See
docs/design/dashboard-authentication.md.

Revision ID: cc1565a6df1e
Revises: d4f2a8c61b93
Create Date: 2026-08-06 15:37:45.507176

"""

import sqlalchemy as sa
from alembic import op

revision = "cc1565a6df1e"
down_revision = "d4f2a8c61b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_users",
        sa.Column("id", sa.Integer(), nullable=False),
        # BigInteger: Telegram documents IDs as fitting in 52 bits.
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "revoked",
                name="dashboard_user_status",
                native_enum=False,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_dashboard_users_telegram_id"),
        "dashboard_users",
        ["telegram_id"],
        unique=True,
    )
    op.create_index(op.f("ix_dashboard_users_status"), "dashboard_users", ["status"], unique=False)

    op.create_table(
        "dashboard_login_nonces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_dashboard_login_nonces_nonce"),
        "dashboard_login_nonces",
        ["nonce"],
        unique=True,
    )
    op.create_index(
        op.f("ix_dashboard_login_nonces_expires_at"),
        "dashboard_login_nonces",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "dashboard_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["dashboard_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_dashboard_sessions_token_hash"),
        "dashboard_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_dashboard_sessions_user_id"), "dashboard_sessions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_dashboard_sessions_expires_at"),
        "dashboard_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_dashboard_sessions_expires_at"), table_name="dashboard_sessions")
    op.drop_index(op.f("ix_dashboard_sessions_user_id"), table_name="dashboard_sessions")
    op.drop_index(op.f("ix_dashboard_sessions_token_hash"), table_name="dashboard_sessions")
    op.drop_table("dashboard_sessions")
    op.drop_index(op.f("ix_dashboard_login_nonces_expires_at"), table_name="dashboard_login_nonces")
    op.drop_index(op.f("ix_dashboard_login_nonces_nonce"), table_name="dashboard_login_nonces")
    op.drop_table("dashboard_login_nonces")
    op.drop_index(op.f("ix_dashboard_users_status"), table_name="dashboard_users")
    op.drop_index(op.f("ix_dashboard_users_telegram_id"), table_name="dashboard_users")
    op.drop_table("dashboard_users")
