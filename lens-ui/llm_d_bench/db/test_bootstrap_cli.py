"""Tests for ``python -m llm_d_bench.db.bootstrap_cli`` -- the installer's
non-interactive replacement for the old in-wizard database setup gate. See
``llm_d_bench/db/bootstrap_cli.py`` and
``scripts/LensInstaller-Ubuntu-x86_64.sh``.
"""

from __future__ import annotations

import os

import pytest

from llm_d_bench.db import bootstrap_cli
from llm_d_bench.db.bootstrap_config import load_bootstrap_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_D_BENCH_DB_MODE", raising=False)
    monkeypatch.delenv("LLM_D_BENCH_EMBEDDED_DB_PASSWORD", raising=False)
    monkeypatch.setenv("LLM_D_BENCH_DB_BOOTSTRAP_FILE", str(tmp_path / "db-bootstrap.json"))
    yield
    # apply_database_setup mutates os.environ directly; pop straight from
    # os.environ (see the identical note in test_system_router.py).
    os.environ.pop("LLM_D_BENCH_DATABASE_URL", None)
    os.environ.pop("LLM_D_BENCH_DB_MODE", None)
    os.environ.pop("LLM_D_BENCH_EMBEDDED_DB_PASSWORD", None)


def test_embedded_mode_configures_and_persists_bootstrap_file():
    exit_code = bootstrap_cli.main(["--mode", "embedded"])

    assert exit_code == 0
    config = load_bootstrap_config()
    assert config is not None
    assert config.mode == "embedded"
    assert config.database_url is None


def test_is_idempotent_by_default():
    assert bootstrap_cli.main(["--mode", "embedded"]) == 0
    configured_at = load_bootstrap_config().configured_at

    exit_code = bootstrap_cli.main(["--mode", "embedded"])

    assert exit_code == 0
    # Second call must be a no-op (skip), not re-run the migration/bootstrap.
    assert load_bootstrap_config().configured_at == configured_at


def test_force_reconfigures_even_when_already_configured():
    assert bootstrap_cli.main(["--mode", "embedded"]) == 0

    exit_code = bootstrap_cli.main(["--mode", "embedded", "--force"])

    assert exit_code == 0


def test_external_mode_requires_host_and_dbname():
    exit_code = bootstrap_cli.main(["--mode", "external", "--engine", "postgresql"])

    assert exit_code == 2
    assert load_bootstrap_config() is None


def test_external_mode_reports_connection_failure_without_persisting():
    exit_code = bootstrap_cli.main(
        [
            "--mode",
            "external",
            "--engine",
            "postgresql",
            "--host",
            "127.0.0.1",
            "--port",
            "1",  # nothing listens here -- connection must fail fast
            "--dbname",
            "prism",
        ]
    )

    assert exit_code == 1
    assert load_bootstrap_config() is None


def test_test_connection_requires_external_mode_host_and_dbname():
    exit_code = bootstrap_cli.main(["--test-connection", "--mode", "embedded"])

    assert exit_code == 2
    assert load_bootstrap_config() is None


def test_test_connection_success_does_not_persist_anything(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap_cli, "_probe_external_connection", lambda _url: None)

    exit_code = bootstrap_cli.main(
        [
            "--test-connection",
            "--mode",
            "external",
            "--engine",
            "postgresql",
            "--host",
            "db.example.com",
            "--dbname",
            "prism",
        ]
    )

    assert exit_code == 0
    assert "connection ok" in capsys.readouterr().out
    # A pure connectivity probe -- must not write env vars or the bootstrap file.
    assert load_bootstrap_config() is None
    assert "LLM_D_BENCH_DATABASE_URL" not in os.environ
    assert "LLM_D_BENCH_DB_MODE" not in os.environ


def test_test_connection_failure_does_not_persist_anything():
    exit_code = bootstrap_cli.main(
        [
            "--test-connection",
            "--mode",
            "external",
            "--engine",
            "postgresql",
            "--host",
            "127.0.0.1",
            "--port",
            "1",  # nothing listens here -- connection must fail fast
            "--dbname",
            "prism",
        ]
    )

    assert exit_code == 1
    assert load_bootstrap_config() is None


def test_embedded_password_is_persisted_and_enforced(monkeypatch, tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    from llm_d_bench.db import settings as db_settings

    monkeypatch.setenv("LLM_D_BENCH_EMBEDDED_DB_DIR", str(tmp_path / "pgdata"))
    monkeypatch.setattr(db_settings, "_embedded_server", None)

    exit_code = bootstrap_cli.main(["--mode", "embedded", "--embedded-password", "correct-horse"])
    assert exit_code == 0

    config = load_bootstrap_config()
    assert config is not None
    assert config.embedded_password == "correct-horse"  # noqa: S105 -- test fixture, not a real credential

    settings = db_settings.DatabaseSettings.from_environment()
    assert settings.embedded_password == "correct-horse"  # noqa: S105 -- test fixture, not a real credential
    # resolve_database_url() returns a SQLAlchemy-style DSN (e.g.
    # "postgresql+psycopg://...", see _normalize_driver) -- use
    # create_engine() rather than a raw psycopg.connect(), which doesn't
    # understand the "+psycopg" driver suffix.
    correct_uri = db_settings.resolve_database_url(settings)

    engine = create_engine(correct_uri)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()

    wrong_uri = correct_uri.replace("correct-horse", "wrong-password")
    wrong_engine = create_engine(wrong_uri)
    try:
        with pytest.raises(OperationalError, match="password authentication failed"), wrong_engine.connect():
            pass
    finally:
        wrong_engine.dispose()
