"""Master-key store, resolution and rotation tests (design §12)."""

from __future__ import annotations

import json

import pytest

from llm_d_bench.auth import master_key
from llm_d_bench.auth.records import IdentityProviderRecord
from llm_d_bench.auth.security import SecretCipher
from llm_d_bench.auth.settings import AuthSettings
from llm_d_bench.db.dao.identity_provider import IdentityProviderDao

#: Non-credential test fixtures; constants avoid S106 literal warnings.
ENV_KEY = "env-key"
FILE_KEY = "file-key"
OLD_KEY = "old-key"
NEW_KEY = "new-key"


def _settings(**overrides) -> AuthSettings:
    values = {"auth_mode": "local", "auto_seed_admin": False}
    values.update(overrides)
    return AuthSettings(**values)


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    path = tmp_path / "credentials" / "master_key.json"
    monkeypatch.setattr(master_key, "master_key_path", lambda: path)
    return path


def test_ensure_generates_and_persists_a_restrictive_file(key_path):
    settings = _settings()
    first = master_key.ensure_master_key(settings)

    assert first.primary
    assert first.source == "file"
    data = json.loads(key_path.read_text(encoding="utf-8"))
    assert data["primary"] == first.primary
    assert (key_path.stat().st_mode & 0o777) == 0o600

    # Idempotent: a second startup keeps the same key.
    assert master_key.ensure_master_key(settings).primary == first.primary


def test_environment_key_overrides_the_stored_key(key_path):
    master_key._write_store(FILE_KEY, ())
    resolved = master_key.resolve_master_key(_settings(secret_key=ENV_KEY))
    assert resolved.primary == "env-key"
    assert resolved.source == "environment"
    assert master_key.status(_settings(secret_key=ENV_KEY))["envLocked"] is True


def test_old_keys_from_env_are_fallbacks(key_path):
    master_key._write_store(FILE_KEY, ())
    resolved = master_key.resolve_master_key(_settings(secret_key=ENV_KEY, old_secret_keys=(FILE_KEY,)))
    assert resolved.primary == "env-key"
    assert resolved.old == ("file-key",)


def test_rotate_reencrypts_and_retains_the_old_key(key_path):
    master_key._write_store(OLD_KEY, ())
    dao = IdentityProviderDao()
    dao.create(
        IdentityProviderRecord(
            type="ldap",
            name="corp",
            enabled=True,
            config={"server_url": "ldaps://ldap.example.org"},
            secret_encrypted=SecretCipher(OLD_KEY).encrypt("bind-password"),
        )
    )

    result = master_key.rotate_master_key(_settings(), NEW_KEY, provider_dao=dao)

    assert result["rotatedSecrets"] == 1
    assert result["oldKeyCount"] == 1
    store = json.loads(key_path.read_text(encoding="utf-8"))
    assert store["primary"] == NEW_KEY
    assert store["old"] == [OLD_KEY]

    record = dao.list()[0]
    assert SecretCipher(NEW_KEY).decrypt(record.secret_encrypted) == "bind-password"
    # The previous key stays available as a decryption fallback.
    assert "old-key" in master_key.resolve_master_key(_settings()).old


def test_rotate_refuses_when_environment_locks_the_key(key_path):
    master_key._write_store(OLD_KEY, ())
    with pytest.raises(Exception, match="environment"):
        master_key.rotate_master_key(_settings(secret_key=ENV_KEY), NEW_KEY, provider_dao=IdentityProviderDao())


def test_clear_old_keys_drops_fallbacks(key_path):
    master_key._write_store(OLD_KEY, ())
    master_key.rotate_master_key(_settings(), NEW_KEY, provider_dao=IdentityProviderDao())

    cleared = master_key.clear_old_keys(_settings())
    assert cleared["oldKeyCount"] == 0
    assert master_key.resolve_master_key(_settings()).old == ()


def test_rotate_without_a_stored_key_is_rejected(key_path):
    with pytest.raises(Exception, match="no stored master key"):
        master_key.rotate_master_key(_settings(), NEW_KEY, provider_dao=IdentityProviderDao())
