from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


CREDENTIAL_TOKEN_PREFIX = "fernet:v1:"


class CredentialEncryptionError(RuntimeError):
    """A credential cannot be encrypted or decrypted safely."""


def _parse_fernet(key: str, setting_name: str) -> Fernet:
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise CredentialEncryptionError(f"{setting_name} contains an invalid key") from error


def _primary_fernet() -> Fernet:
    configured = settings.credential_encryption_key
    key = configured.get_secret_value().strip() if configured is not None else ""
    if not key:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is required when WordPress credentials are stored"
        )
    return _parse_fernet(key, "CREDENTIAL_ENCRYPTION_KEY")


def _legacy_fernets() -> list[Fernet]:
    configured = settings.credential_decryption_keys
    raw = configured.get_secret_value() if configured is not None else ""
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    return [_parse_fernet(key, "CREDENTIAL_DECRYPTION_KEYS") for key in keys]


def validate_credential_encryption_key() -> None:
    """Fail early without encrypting or exposing a credential."""
    _primary_fernet()


def is_encrypted_credential(value: str) -> bool:
    return value.startswith(CREDENTIAL_TOKEN_PREFIX)


def encrypt_credential(value: str) -> str:
    """Return a versioned authenticated ciphertext, never the plaintext value."""
    if not value:
        return value
    token = _primary_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return CREDENTIAL_TOKEN_PREFIX + token


def decrypt_credential(value: str) -> str:
    """Decrypt a versioned token; never silently return plaintext from storage."""
    if not value:
        return value
    if not is_encrypted_credential(value):
        raise CredentialEncryptionError(
            "stored WordPress credential is not encrypted; run the credential migration"
        )
    token = value.removeprefix(CREDENTIAL_TOKEN_PREFIX)
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError as error:
        raise CredentialEncryptionError("stored WordPress credential is invalid") from error

    for fernet in [_primary_fernet(), *_legacy_fernets()]:
        try:
            return fernet.decrypt(encoded).decode("utf-8")
        except InvalidToken:
            continue
        except UnicodeDecodeError as error:
            raise CredentialEncryptionError("stored WordPress credential is invalid") from error
    raise CredentialEncryptionError(
        "stored WordPress credential cannot be decrypted with the configured keys"
    )
