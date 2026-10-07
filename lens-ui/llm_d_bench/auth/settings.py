"""Auth runtime settings, loaded from the environment.

Follows the repository's ``frozen dataclass + from_environment()`` convention
(see ``llm_d_bench/cluster/settings.py``). Design reference: section 13.1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

#: ``disabled`` fully bypasses authentication/authorization for local dev and
#: tests (see ``AuthSettings.disabled``). Any other value means "enabled":
#: ``AuthService.login`` decides local-vs-directory per username by looking at
#: that account's own ``auth_source`` — there is no local/external/hybrid mode
#: to configure.
VALID_AUTH_MODES = ("local", "disabled")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_key_list(name: str) -> tuple[str, ...]:
    """Parse a comma-separated key list, trimming blanks and de-duplicating."""
    raw = os.environ.get(name, "")
    keys = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class AuthSettings:
    #: ``"disabled"`` bypasses auth entirely (dev/test); any other value is a
    #: no-op placeholder — see ``VALID_AUTH_MODES`` and ``AuthService.login``.
    auth_mode: str = "local"
    allow_unauthenticated: bool = False
    secret_key: str = ""
    #: Old master keys (comma-separated in LENS_SECRET_KEYS_OLD), tried after
    #: ``secret_key`` when decrypting stored provider secrets (design §12).
    old_secret_keys: tuple[str, ...] = ()
    internal_auth_secret: str = ""
    # Absolute cap on a session's lifetime, independent of activity (prevents
    # infinite sliding renewal). Deliberately much larger than the idle
    # timeout below: the practical "log me out" signal for an active user is
    # inactivity, not the clock since login.
    session_ttl_seconds: int = 43200
    # Sliding idle timeout: a session with no request in this many seconds is
    # invalid, even though the absolute cap above hasn't been reached yet.
    session_idle_seconds: int = 1800
    remember_session_ttl_seconds: int = 2592000
    remember_session_idle_seconds: int = 2592000
    session_touch_interval_seconds: int = 60
    session_max_per_user: int = 10
    #: Only one active session per account: a new login revokes the previous one.
    session_single_active: bool = True
    session_revoked_retention_seconds: int = 86400
    session_sweep_interval_seconds: int = 900
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_lockout_seconds: int = 900
    audit_retention_days: int = 180
    expose_api_docs: bool = True
    auto_seed_admin: bool = True
    initial_admin_username: str = "admin"
    initial_admin_password: str = ""

    @classmethod
    def from_environment(cls) -> AuthSettings:
        mode = (os.environ.get("PRISM_AUTH_MODE") or "local").strip().lower()
        allow_unauthenticated = _env_bool("PRISM_ALLOW_UNAUTHENTICATED", False)
        # Legacy alias (design section 5.1): treated as disabled auth.
        if _env_bool("SIMULATION_ALLOW_UNAUTHENTICATED", False):
            mode = "disabled"
            allow_unauthenticated = True
        if mode not in VALID_AUTH_MODES:
            mode = "local"
        return cls(
            auth_mode=mode,
            allow_unauthenticated=allow_unauthenticated,
            secret_key=os.environ.get("LENS_SECRET_KEY", ""),
            old_secret_keys=_env_key_list("LENS_SECRET_KEYS_OLD"),
            internal_auth_secret=os.environ.get("LENS_INTERNAL_AUTH_SECRET", ""),
            session_ttl_seconds=_env_int("PRISM_SESSION_TTL_SECONDS", 43200),
            session_idle_seconds=_env_int("PRISM_SESSION_IDLE_SECONDS", 1800),
            remember_session_ttl_seconds=_env_int("PRISM_REMEMBER_SESSION_TTL_SECONDS", 2592000),
            remember_session_idle_seconds=_env_int("PRISM_REMEMBER_SESSION_IDLE_SECONDS", 2592000),
            session_touch_interval_seconds=_env_int("PRISM_SESSION_TOUCH_INTERVAL_SECONDS", 60),
            session_max_per_user=_env_int("PRISM_SESSION_MAX_PER_USER", 10),
            session_single_active=_env_bool("PRISM_SESSION_SINGLE_ACTIVE", True),
            session_revoked_retention_seconds=_env_int("PRISM_SESSION_REVOKED_RETENTION_SECONDS", 86400),
            session_sweep_interval_seconds=_env_int("PRISM_SESSION_SWEEP_INTERVAL_SECONDS", 900),
            cookie_secure=_env_bool("PRISM_COOKIE_SECURE", True),
            login_max_failures=_env_int("PRISM_LOGIN_MAX_FAILURES", 5),
            login_lockout_seconds=_env_int("PRISM_LOGIN_LOCKOUT_SECONDS", 900),
            audit_retention_days=_env_int("PRISM_AUDIT_RETENTION_DAYS", 180),
            expose_api_docs=_env_bool("PRISM_EXPOSE_API_DOCS", True),
            auto_seed_admin=_env_bool("PRISM_ADMIN_AUTOSEED", True),
            initial_admin_username=os.environ.get("PRISM_ADMIN_USERNAME", "admin"),
            initial_admin_password=os.environ.get("PRISM_ADMIN_PASSWORD", ""),
        )

    @property
    def disabled(self) -> bool:
        return self.auth_mode == "disabled"

    def as_public_dict(self) -> dict[str, Any]:
        """Non-sensitive subset safe to log or return."""
        hidden = {"secret_key", "old_secret_keys", "internal_auth_secret"}
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in hidden}


settings = AuthSettings.from_environment()

_override: AuthSettings | None = None


def get_settings() -> AuthSettings:
    """Current settings (test override wins, else the environment-derived default)."""
    return _override if _override is not None else settings


def set_settings_for_testing(replacement: AuthSettings | None) -> None:
    """Override settings for a test; always reset to ``None`` afterward."""
    global _override
    _override = replacement


def reset_settings_override() -> None:
    set_settings_for_testing(None)
