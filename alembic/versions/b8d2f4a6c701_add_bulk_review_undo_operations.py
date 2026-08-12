"""add durable bulk review undo operations

Revision ID: b8d2f4a6c701
Revises: a7c9e1f3b506
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d2f4a6c701"
down_revision = "a7c9e1f3b506"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bulk_review_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=False),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("undone_count", sa.Integer(), nullable=True),
        sa.Column("skipped_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bulk_review_operations_created_at",
        "bulk_review_operations",
        ["created_at"],
    )
    op.create_table(
        "bulk_review_operation_items",
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["bulk_review_operations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["suggestion_id"], ["suggestions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operation_id", "suggestion_id"),
    )
    op.create_index(
        "ix_bulk_review_operation_items_suggestion_id",
        "bulk_review_operation_items",
        ["suggestion_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bulk_review_operation_items_suggestion_id",
        table_name="bulk_review_operation_items",
    )
    op.drop_table("bulk_review_operation_items")
    op.drop_index("ix_bulk_review_operations_created_at", table_name="bulk_review_operations")
    op.drop_table("bulk_review_operations")
