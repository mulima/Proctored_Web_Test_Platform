"""Encrypting a lecturer's own database connection string at rest.

Deliberately a separate key from SECRET_KEY (app/config.py): SECRET_KEY signs
session cookies and email-verification tokens and is expected to be stable
but not precious - rotating it just signs everyone out. CREDENTIAL_ENCRYPTION_KEY
protects the one thing in the platform database that is a live credential to
someone else's infrastructure; rotating *that* would silently corrupt every
stored connection string, so the two must never be tied together.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CredentialEncryptionError(Exception):
    pass


@lru_cache
def _fernet() -> Fernet:
    key = settings.credential_encryption_key
    if not key:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and set it as an environment variable before any lecturer can connect a database."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key."
        ) from exc


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Almost always means CREDENTIAL_ENCRYPTION_KEY changed since this row was
        # written - treat it as "the stored connection string is gone", not a crash.
        raise CredentialEncryptionError(
            "Stored database connection string could not be decrypted. If "
            "CREDENTIAL_ENCRYPTION_KEY changed recently, the lecturer needs to "
            "reconnect their database."
        ) from exc
