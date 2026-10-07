"""First-run administrator bootstrap (design section 5.4).

On the first startup with no users, this creates a single global ``admin`` and
surfaces its credentials so the operator can sign in once:

- ``PRISM_ADMIN_USERNAME`` / ``PRISM_ADMIN_PASSWORD`` may supply them; and
- otherwise a strong password is generated, written to
    ``<LENS_DATA_DIR>/credentials/initial_admin.txt`` (mode 0600).

The account is created with ``must_change_password=True``; the file is removed
once that password is changed.
"""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

from llm_d_bench.auth.records import UserRoleBindingRecord
from llm_d_bench.auth.service import AuthService, validate_password_strength
from llm_d_bench.auth.settings import AuthSettings
from llm_d_bench.utils.paths import storage_path

logger = logging.getLogger(__name__)

INITIAL_ADMIN_FILENAME = "initial_admin.txt"
_PASSWORD_LENGTH = 20
_SYMBOLS = "!@#$%^&*()-_=+[]{}"


@dataclass(frozen=True)
class InitialAdmin:
    username: str
    password: str | None
    generated: bool
    path: Path | None


def initial_admin_path() -> Path:
    return storage_path("data", "credentials", INITIAL_ADMIN_FILENAME)


def generate_password() -> str:
    pool = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        candidate = "".join(secrets.choice(pool) for _ in range(_PASSWORD_LENGTH))
        try:
            validate_password_strength(candidate)
        except Exception:  # noqa: BLE001, S112 - retry until the policy is satisfied
            continue
        return candidate


def write_operator_credentials(username: str, password: str) -> Path:
    """Write generated administrator credentials to the protected handoff file."""
    path = initial_admin_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        "# Lens administrator credentials.\n"
        "# Sign in once, then change this password; this file is deleted on change.\n"
        f"username={username}\n"
        f"password={password}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def clear_initial_admin_credentials() -> None:
    """Best-effort removal once the initial password has been changed."""
    try:
        path = initial_admin_path()
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("could not remove initial admin credential file", exc_info=True)


def ensure_initial_admin(service: AuthService, settings: AuthSettings) -> InitialAdmin | None:
    """Create the first administrator when the database has no users yet."""
    if not settings.auto_seed_admin or settings.disabled:
        return None
    if service.user_count() > 0:
        return None
    service.ensure_builtin_roles()

    username = (settings.initial_admin_username or "admin").strip() or "admin"
    generated = not settings.initial_admin_password
    password = settings.initial_admin_password or generate_password()

    user = service.create_user(
        username=username,
        password=password,
        display_name=username,
        must_change_password=True,
        validate_password=generated,
    )
    admin_role = service.require_role("admin")
    service.user_binding_dao.create(UserRoleBindingRecord(user_id=user.id, role_id=admin_role.id, scope_type="global"))
    service.user_dao.bump_principal_version(user.id)
    service.audit("bootstrap", actor=user)

    path = write_operator_credentials(username, password) if generated else None
    return InitialAdmin(username=username, password=password if generated else None, generated=generated, path=path)


def format_initial_admin_banner(initial: InitialAdmin) -> str:
    lines = [
        "",
        "=" * 64,
        " Lens initial administrator (first run)",
        f"   username: {initial.username}",
    ]
    if initial.generated:
        lines.append(f"   credentials: generated and saved to {initial.path} (mode 600)")
    else:
        lines.append("   credentials: provided through PRISM_ADMIN_PASSWORD")
    lines.extend(
        [
            " Change this password immediately after signing in.",
            "=" * 64,
            "",
        ]
    )
    return "\n".join(lines)
