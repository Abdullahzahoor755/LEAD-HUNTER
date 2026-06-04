"""Lightweight encryption helpers for tenant-scoped provider secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
from itertools import cycle

from app.configs.settings import settings


class SecretEncryptionError(ValueError):
    """Raised when provider secret encryption configuration is invalid."""


def _secret_key_bytes() -> bytes:
    secret = str(settings.secret_encryption_key or "").strip()
    if not secret:
        raise SecretEncryptionError("SECRET_ENCRYPTION_KEY is required for provider credential storage.")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt_secret(value: str) -> str:
    raw = str(value or "").encode("utf-8")
    key = _secret_key_bytes()
    cipher = bytes(source ^ key_byte for source, key_byte in zip(raw, cycle(key)))
    signature = hmac.new(key, cipher, hashlib.sha256).hexdigest()
    return f"{signature}:{base64.urlsafe_b64encode(cipher).decode('ascii')}"


def decrypt_secret(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    signature, _, payload = token.partition(":")
    cipher = base64.urlsafe_b64decode(payload.encode("ascii"))
    key = _secret_key_bytes()
    expected = hmac.new(key, cipher, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise SecretEncryptionError("Provider secret signature check failed.")
    plain = bytes(source ^ key_byte for source, key_byte in zip(cipher, cycle(key)))
    return plain.decode("utf-8")
