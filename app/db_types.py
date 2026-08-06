from sqlalchemy import Text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from app.security.credentials import decrypt_credential, encrypt_credential


class EncryptedCredential(TypeDecorator[str]):
    """Encrypt values on writes and expose plaintext only inside the application."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return encrypt_credential(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return decrypt_credential(value)
