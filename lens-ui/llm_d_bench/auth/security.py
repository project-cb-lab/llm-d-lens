"""Credential, session-token and secret primitives for the auth domain.

Kept free of persistence and transport so it can be reused by the service
layer and unit-tested in isolation. Design references: sections 5.2, 5.3, 8.3
and 12.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Iterable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

SESSION_TOKEN_BYTES = 32
INTERNAL_AUTH_WINDOW_SECONDS = 60

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password with Argon2id (the library default profile)."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time password verification; never raises on mismatch."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, ValueError):
        return False


def generate_session_token() -> str:
    """Generate a new opaque session token (only its hash is persisted)."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """SHA-256 hash used for session-token lookup in the ``sessions`` table."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _internal_message(timestamp: int, method: str, path: str, principal_id: str) -> bytes:
    return f"{timestamp}\n{method.upper()}\n{path}\n{principal_id}".encode()


def sign_internal(secret: str, *, timestamp: int, method: str, path: str, principal_id: str) -> str:
    """Sign the Node->Python principal assertion (design section 8.3)."""
    return hmac.new(
        secret.encode("utf-8"),
        _internal_message(timestamp, method, path, principal_id),
        hashlib.sha256,
    ).hexdigest()


def verify_internal(
    secret: str,
    *,
    signature: str,
    timestamp: int,
    method: str,
    path: str,
    principal_id: str,
    now: int | None = None,
    window_seconds: int = INTERNAL_AUTH_WINDOW_SECONDS,
) -> bool:
    """Verify signature freshness and integrity in constant time."""
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > window_seconds:
        return False
    expected = sign_internal(secret, timestamp=timestamp, method=method, path=path, principal_id=principal_id)
    return hmac.compare_digest(expected, signature)


def _fernet_for(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


class SecretCipher:
    """Symmetric encryption for stored secrets with key-rotation fallback.

    ``primary`` encrypts new values; ``old`` keys are tried on decrypt so a key
    can be rotated without re-encrypting everything at once (design section
    12). Raises ``ValueError`` when no key can decrypt.
    """

    def __init__(self, primary: str, old: Iterable[str] = ()):  # noqa: S107 - secret passed by caller
        if not primary:
            raise ValueError("a non-empty primary secret key is required")
        self._primary = _fernet_for(primary)
        self._fallbacks = [_fernet_for(key) for key in old if key]

    def encrypt(self, plaintext: str) -> str:
        return self._primary.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        for fernet in (self._primary, *self._fallbacks):
            try:
                return fernet.decrypt(token.encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError):
                continue
        raise ValueError("ciphertext could not be decrypted with any configured key")
