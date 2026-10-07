"""Stable, secret-safe hashing for immutable deployment evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_SENSITIVE_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "password",
    "secret",
    "kubeconfig",
}


def stable_hash(payload: dict[str, Any]) -> str:
    cleaned = _sanitize(payload)
    serialized = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if str(key).lower() in _SENSITIVE_KEYS else _sanitize(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value
