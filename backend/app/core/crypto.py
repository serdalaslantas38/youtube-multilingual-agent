"""Symmetric encryption for OAuth refresh tokens at rest.

Uses `cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256 with timestamp).
- Accepts either a standard 32-byte urlsafe-base64 Fernet key or derives a
  deterministic Fernet key via SHA-256 from a high-entropy passphrase when
  configured in local development.
- Refresh tokens are NEVER stored in plaintext or returned in API responses/logs.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class TokenEncryptionError(Exception):
    """Raised when token encryption or decryption fails or no key is configured."""


def _build_fernet(secret_key: SecretStr | str) -> Fernet:
    raw = (
        secret_key.get_secret_value().strip()
        if isinstance(secret_key, SecretStr)
        else secret_key.strip()
    )
    if not raw:
        raise TokenEncryptionError(
            "ENCRYPTION_KEY is not configured; refusing to store or read OAuth refresh tokens."
        )
    raw_bytes = raw.encode("utf-8")
    try:
        return Fernet(raw_bytes)
    except ValueError:
        if len(raw) < 16:
            raise TokenEncryptionError(
                "ENCRYPTION_KEY must be a valid 32-byte urlsafe-base64 Fernet key "
                "or a secret string of at least 16 characters."
            ) from None
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw_bytes).digest())
        return Fernet(derived)


def encrypt_secret(plaintext: str, encryption_key: SecretStr | str) -> str:
    """Encrypt a sensitive string (such as an OAuth refresh token) for storage at rest."""
    cleaned = plaintext.strip()
    if not cleaned:
        raise TokenEncryptionError("Cannot encrypt an empty secret token.")
    fernet = _build_fernet(encryption_key)
    return fernet.encrypt(cleaned.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, encryption_key: SecretStr | str) -> str:
    """Decrypt a stored ciphertext back into its plaintext secret."""
    cleaned = ciphertext.strip()
    if not cleaned:
        raise TokenEncryptionError("Cannot decrypt an empty ciphertext.")
    fernet = _build_fernet(encryption_key)
    try:
        return fernet.decrypt(cleaned.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenEncryptionError(
            "Failed to decrypt stored OAuth token (invalid key or corrupted ciphertext)."
        ) from exc
