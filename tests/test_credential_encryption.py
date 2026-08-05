from pydantic import SecretStr
import pytest
from sqlalchemy import text

from app.config import settings
from app.models import Site
from app.security.credentials import (
    CREDENTIAL_TOKEN_PREFIX,
    CredentialEncryptionError,
    decrypt_credential,
    encrypt_credential,
)


PRIMARY_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
ROTATED_KEY = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="


def test_credential_round_trip_is_authenticated_and_not_plaintext():
    encrypted = encrypt_credential("wordpress-app-password")

    assert encrypted.startswith(CREDENTIAL_TOKEN_PREFIX)
    assert "wordpress-app-password" not in encrypted
    assert decrypt_credential(encrypted) == "wordpress-app-password"


def test_plaintext_read_compatibility_during_migration():
    assert decrypt_credential("legacy-plaintext") == "legacy-plaintext"


def test_wrong_key_cannot_decrypt(monkeypatch):
    encrypted = encrypt_credential("secret")
    monkeypatch.setattr(
        settings,
        "credential_encryption_key",
        SecretStr(ROTATED_KEY),
    )

    with pytest.raises(CredentialEncryptionError, match="cannot be decrypted"):
        decrypt_credential(encrypted)


def test_rotation_reads_old_tokens_but_writes_only_with_new_primary(monkeypatch):
    old_token = encrypt_credential("secret-before-rotation")
    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr(ROTATED_KEY))
    monkeypatch.setattr(settings, "credential_decryption_keys", SecretStr(PRIMARY_KEY))

    assert decrypt_credential(old_token) == "secret-before-rotation"
    new_token = encrypt_credential("secret-after-rotation")

    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr(PRIMARY_KEY))
    monkeypatch.setattr(settings, "credential_decryption_keys", None)
    with pytest.raises(CredentialEncryptionError, match="configured keys"):
        decrypt_credential(new_token)


def test_rotation_accepts_multiple_previous_keys(monkeypatch):
    old_token = encrypt_credential("secret")
    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr(ROTATED_KEY))
    monkeypatch.setattr(
        settings,
        "credential_decryption_keys",
        SecretStr(f"{ROTATED_KEY},{PRIMARY_KEY}"),
    )

    assert decrypt_credential(old_token) == "secret"


def test_missing_key_refuses_credential_storage(monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", None)

    with pytest.raises(CredentialEncryptionError, match="is required"):
        encrypt_credential("secret")


def test_api_rejects_wordpress_credentials_when_key_is_missing(client, monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", None)

    response = client.post(
        "/api/v1/sites",
        json={
            "name": "missing-key-site",
            "base_url": "https://missing-key.example.com",
            "platform": "wordpress",
            "wp_username": "editor",
            "wp_app_password": "wordpress-app-password",
        },
    )

    assert response.status_code == 422
    assert "CREDENTIAL_ENCRYPTION_KEY is required" in response.text


def test_site_password_is_ciphertext_in_database_but_plaintext_in_application(db):
    site = Site(
        name="encrypted-site",
        base_url="https://encrypted-credentials.example.com",
        platform="wordpress",
        wp_username="editor",
        wp_app_password="wordpress-app-password",
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    stored = db.execute(
        text("SELECT wp_app_password FROM sites WHERE id = :site_id"),
        {"site_id": site.id},
    ).scalar_one()
    assert stored.startswith(CREDENTIAL_TOKEN_PREFIX)
    assert "wordpress-app-password" not in stored
    assert site.wp_app_password == "wordpress-app-password"

    db.delete(site)
    db.commit()


def test_site_api_never_returns_password(client):
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "api-encrypted-site",
            "base_url": "https://api-encrypted-credentials.example.com",
            "platform": "wordpress",
            "wp_username": "editor",
            "wp_app_password": "wordpress-app-password",
        },
    )

    assert response.status_code == 201
    assert "wp_app_password" not in response.json()
    client.delete(f"/api/v1/sites/{response.json()['id']}")
