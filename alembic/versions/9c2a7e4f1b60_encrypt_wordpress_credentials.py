"""encrypt WordPress application passwords at rest

Revision ID: 9c2a7e4f1b60
Revises: c5f3a91b7d24
Create Date: 2026-08-03 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

from app.security.credentials import encrypt_credential, is_encrypted_credential


revision = "9c2a7e4f1b60"
down_revision = "c5f3a91b7d24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fernet ciphertext is longer than the original 255-character plaintext limit.
    op.alter_column(
        "sites",
        "wp_app_password",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )

    connection = op.get_bind()
    credentials = connection.execute(
        sa.text("SELECT id, wp_app_password FROM sites WHERE wp_app_password IS NOT NULL")
    ).all()
    for site_id, credential in credentials:
        if is_encrypted_credential(credential):
            continue
        connection.execute(
            sa.text("UPDATE sites SET wp_app_password = :credential WHERE id = :site_id"),
            {"credential": encrypt_credential(credential), "site_id": site_id},
        )


def downgrade() -> None:
    connection = op.get_bind()
    credential_count = connection.scalar(
        sa.text("SELECT count(*) FROM sites WHERE wp_app_password IS NOT NULL")
    )
    if credential_count:
        raise RuntimeError(
            "Refusing to downgrade while encrypted WordPress credentials exist. "
            "Clear the credentials explicitly before removing at-rest encryption."
        )
    op.alter_column(
        "sites",
        "wp_app_password",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
