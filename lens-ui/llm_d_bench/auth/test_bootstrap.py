"""First-run administrator bootstrap tests (design section 5.4)."""

from __future__ import annotations

from llm_d_bench.auth.bootstrap import (
    clear_initial_admin_credentials,
    ensure_initial_admin,
    format_initial_admin_banner,
)
from llm_d_bench.auth.security import verify_password
from llm_d_bench.auth.service import AuthService, validate_password_strength
from llm_d_bench.auth.settings import AuthSettings


def _service(**overrides) -> AuthService:
    settings = AuthSettings(auth_mode="local", auto_seed_admin=True, **overrides)
    return AuthService(settings)


def test_generated_admin_is_seeded_flagged_and_written(monkeypatch, tmp_path):
    from llm_d_bench.auth import bootstrap

    cred_file = tmp_path / "credentials" / "initial_admin.txt"
    monkeypatch.setattr(bootstrap, "initial_admin_path", lambda: cred_file)

    service = _service(initial_admin_username="root")
    initial = ensure_initial_admin(service, service.settings)

    assert initial is not None
    assert initial.generated is True
    assert initial.username == "root"
    assert initial.password != "admin"
    validate_password_strength(initial.password, username="root")
    assert cred_file.exists()
    contents = cred_file.read_text(encoding="utf-8")
    assert "username=root" in contents
    assert initial.password in contents
    assert oct(cred_file.stat().st_mode & 0o777) == "0o600"

    admin = service.user_dao.get_by_username("root")
    assert verify_password(initial.password, admin.password_hash)
    assert not verify_password("admin", admin.password_hash)
    assert admin.must_change_password is True
    assert service.principal_for(admin).is_global_admin()

    # Second startup with an existing user does not re-seed.
    assert ensure_initial_admin(service, service.settings) is None

    clear_initial_admin_credentials()
    assert not cred_file.exists()


def test_env_password_is_not_written_to_disk(monkeypatch, tmp_path):
    from llm_d_bench.auth import bootstrap

    cred_file = tmp_path / "credentials" / "initial_admin.txt"
    monkeypatch.setattr(bootstrap, "initial_admin_path", lambda: cred_file)

    service = _service(
        initial_admin_username="ops",
        initial_admin_password="Provided-Pass-9!",  # noqa: S106 - test fixture  # nosemgrep
    )
    initial = ensure_initial_admin(service, service.settings)

    assert initial is not None
    assert initial.generated is False
    assert initial.password is None
    assert initial.path is None
    assert not cred_file.exists()
    assert service.user_dao.get_by_username("ops") is not None


def test_banner_mentions_username_and_change_hint(monkeypatch, tmp_path):
    from llm_d_bench.auth import bootstrap

    monkeypatch.setattr(bootstrap, "initial_admin_path", lambda: tmp_path / "initial_admin.txt")
    service = _service()
    initial = ensure_initial_admin(service, service.settings)
    banner = format_initial_admin_banner(initial)
    assert "username: admin" in banner
    assert initial.password not in banner
    assert str(initial.path) in banner
    assert "password:" not in banner
    assert "Change this password" in banner
