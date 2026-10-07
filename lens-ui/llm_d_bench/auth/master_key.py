"""Master-key lifecycle for stored provider secrets (design §12).

The **master key** (``LENS_SECRET_KEY``) encrypts external-provider secrets
(LDAP bind passwords and future OIDC client secrets) through
:class:`~llm_d_bench.auth.security.SecretCipher`.

Resolution order:

1. The ``LENS_SECRET_KEY`` environment variable, when set, always wins and makes
   the stored key read-only from the UI (``envLocked``).
2. Otherwise the key persisted in
   ``<LENS_DATA_DIR>/credentials/master_key.json`` (mode 0600).
3. Otherwise a fresh key is generated once at startup by :func:`ensure_master_key`.

Rotation re-encrypts every stored provider secret with the new key and keeps the
previous key(s) in the store's ``old`` list so ciphertext written before the
change still decrypts (``LENS_SECRET_KEYS_OLD`` may also supply extra fallback
keys). Old keys can be dropped once confirmed unused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from llm_d_bench.auth.security import SecretCipher
from llm_d_bench.auth.settings import AuthSettings
from llm_d_bench.core.exceptions import DomainValidationError
from llm_d_bench.utils.paths import storage_path

logger = logging.getLogger(__name__)

MASTER_KEY_FILENAME = "master_key.json"
_KEY_BYTES = 48


class ProviderSecretDao(Protocol):
    """Minimal DAO surface needed to re-encrypt provider secrets."""

    def list(self) -> list[Any]: ...

    def save(self, record: Any) -> Any: ...


@dataclass(frozen=True)
class MasterKey:
    primary: str
    old: tuple[str, ...]
    source: str  # "environment" | "file" | "unconfigured"


def master_key_path() -> Path:
    return storage_path("data", "credentials", MASTER_KEY_FILENAME)


def generate_key() -> str:
    """Generate a high-entropy master key (URL-safe, ~64 chars)."""
    return secrets.token_urlsafe(_KEY_BYTES)


def fingerprint(key: str) -> str:
    """Non-reversible short identifier so the UI can tell keys apart."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _read_store() -> MasterKey | None:
    path = master_key_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("master key file %s is unreadable; ignoring it", path, exc_info=True)
        return None
    primary = str(data.get("primary") or "").strip()
    if not primary:
        return None
    old = tuple(str(item).strip() for item in (data.get("old") or []) if str(item).strip())
    return MasterKey(primary=primary, old=old, source="file")


def _write_store(primary: str, old: tuple[str, ...]) -> MasterKey:
    path = master_key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"primary": primary, "old": list(old)}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    path.chmod(0o600)
    return MasterKey(primary=primary, old=old, source="file")


def resolve_master_key(settings: AuthSettings) -> MasterKey:
    """Effective (primary, old) keys without generating or writing anything."""
    env_primary = (settings.secret_key or "").strip()
    old: list[str] = [key for key in settings.old_secret_keys if key]

    stored = _read_store()
    if stored is not None:
        old.extend(stored.old)

    if env_primary:
        return MasterKey(primary=env_primary, old=_dedupe(old, exclude=env_primary), source="environment")
    if stored is not None:
        return MasterKey(
            primary=stored.primary,
            old=_dedupe(old, exclude=stored.primary),
            source="file",
        )
    return MasterKey(primary="", old=_dedupe(old), source="unconfigured")


def _dedupe(keys: list[str], *, exclude: str = "") -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for key in keys:
        if key and key != exclude and key not in seen:
            seen[key] = None
    return tuple(seen)


def ensure_master_key(settings: AuthSettings) -> MasterKey:
    """Load the effective key, generating and persisting one when absent.

    Never overrides an environment-supplied key.
    """
    if (settings.secret_key or "").strip():
        return resolve_master_key(settings)

    stored = _read_store()
    if stored is not None and stored.primary:
        return resolve_master_key(settings)

    path = master_key_path()
    key = generate_key()
    written = _write_store(key, ())
    logger.warning(
        "Generated a new LENS_SECRET_KEY and wrote it to %s (mode 0600). "
        "Back it up: rotating or losing it makes stored provider secrets unreadable.",
        path,
    )
    return MasterKey(primary=written.primary, old=written.old, source="file")


def status(settings: AuthSettings) -> dict[str, Any]:
    """Non-sensitive view of the master key for the admin UI/API."""
    resolved = resolve_master_key(settings)
    return {
        "source": resolved.source,
        "envLocked": bool((settings.secret_key or "").strip()),
        "configured": bool(resolved.primary),
        "fingerprint": fingerprint(resolved.primary) if resolved.primary else "",
        "keyLength": len(resolved.primary),
        "oldKeyCount": len(resolved.old),
        "path": str(master_key_path()),
    }


def rotate_master_key(
    settings: AuthSettings,
    new_key: str | None,
    *,
    provider_dao: ProviderSecretDao,
) -> dict[str, Any]:
    """Re-encrypt stored provider secrets under a new key and retain the old one.

    Returns a status dict extended with ``rotatedSecrets``.
    """
    if (settings.secret_key or "").strip():
        raise DomainValidationError(
            "LENS_SECRET_KEY is provided through the environment; rotate it there instead of the UI"
        )

    stored = _read_store()
    if stored is None:
        raise DomainValidationError("no stored master key to rotate; restart Lens to generate one")

    candidate = (new_key or "").strip() or generate_key()
    if candidate == stored.primary:
        raise DomainValidationError("the new master key must differ from the current one")

    old_cipher = SecretCipher(stored.primary, old=stored.old)
    new_cipher = SecretCipher(candidate)

    updates: list[tuple[Any, str]] = []
    for record in provider_dao.list():
        encrypted = getattr(record, "secret_encrypted", None)
        if not encrypted:
            continue
        try:
            plaintext = old_cipher.decrypt(encrypted)
        except ValueError as error:
            raise DomainValidationError(
                f"cannot decrypt a stored secret for provider '{getattr(record, 'name', '?')}'; "
                "restore the missing old key before rotating"
            ) from error
        updates.append((record, new_cipher.encrypt(plaintext)))

    # Persist the new primary with the previous key retained first, so a partial
    # re-encryption below is always still decryptable via the fallback list.
    _write_store(candidate, _dedupe([stored.primary, *stored.old]))
    for record, encrypted in updates:
        record.secret_encrypted = encrypted
        provider_dao.save(record)

    result = status(settings)
    result["rotatedSecrets"] = len(updates)
    return result


def clear_old_keys(settings: AuthSettings) -> dict[str, Any]:
    """Drop fallback keys; callers must have re-encrypted secrets first."""
    if (settings.secret_key or "").strip():
        raise DomainValidationError("LENS_SECRET_KEY is provided through the environment; the stored copy is unused")

    stored = _read_store()
    if stored is None:
        raise DomainValidationError("no stored master key configured")

    _write_store(stored.primary, ())
    return status(settings)
