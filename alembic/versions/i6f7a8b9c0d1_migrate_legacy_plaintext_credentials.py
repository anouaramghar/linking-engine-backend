"""encrypt any legacy plaintext WordPress credentials

Revision ID: i6f7a8b9c0d1
Revises: h5d6e7f8a9b0
Create Date: 2026-08-16 00:00:00.000000

This is intentionally a data-only follow-up to the original credential
encryption migration. It also covers databases that had already reached the
previous head while the application still accepted plaintext rows.
"""

from alembic import op
import sqlalchemy as sa

from app.security.credentials import encrypt_credential, is_encrypted_credential


revision = "i6f7a8b9c0d1"
down_revision = "h5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    credentials = connection.execute(
        sa.text("SELECT id, wp_app_password FROM sites WHERE wp_app_password IS NOT NULL")
    ).all()
    for site_id, credential in credentials:
        if not credential or is_encrypted_credential(credential):
            continue
        connection.execute(
            sa.text("UPDATE sites SET wp_app_password = :credential WHERE id = :site_id"),
            {"credential": encrypt_credential(credential), "site_id": site_id},
        )


def downgrade() -> None:
    """Keep credentials encrypted; converting them back would be unsafe."""
