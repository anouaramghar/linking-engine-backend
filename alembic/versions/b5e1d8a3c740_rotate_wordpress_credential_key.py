"""re-encrypt WordPress credentials with the primary key

Revision ID: b5e1d8a3c740
Revises: 9c2a7e4f1b60
Create Date: 2026-08-03 13:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

from app.security.credentials import decrypt_credential, encrypt_credential


revision = "b5e1d8a3c740"
down_revision = "9c2a7e4f1b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    credentials = connection.execute(
        sa.text("SELECT id, wp_app_password FROM sites WHERE wp_app_password IS NOT NULL")
    ).all()
    for site_id, credential in credentials:
        plaintext = decrypt_credential(credential)
        connection.execute(
            sa.text("UPDATE sites SET wp_app_password = :credential WHERE id = :site_id"),
            {"credential": encrypt_credential(plaintext), "site_id": site_id},
        )


def downgrade() -> None:
    """Keep credentials on the primary key; returning to an old key is unsafe."""
