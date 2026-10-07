"""Tests for the "Step 0" system database setup API.

See docs/design/cluster-creation-wizard-design.md section 4.0.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.db import system_router
from llm_d_bench.db.bootstrap_config import DatabaseBootstrapConfig

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_D_BENCH_DB_MODE", raising=False)
    monkeypatch.setenv("LLM_D_BENCH_DB_BOOTSTRAP_FILE", str(tmp_path / "db-bootstrap.json"))
    yield
    # The POST handler under test mutates os.environ directly (that is the
    # feature under test); using monkeypatch.delenv here would only get
    # reverted by monkeypatch's own teardown (restoring the leaked value),
    # so pop straight from os.environ instead.
    os.environ.pop("LLM_D_BENCH_DATABASE_URL", None)
    os.environ.pop("LLM_D_BENCH_DB_MODE", None)


def test_status_reports_unconfigured_by_default():
    response = client.get("/api/v1/system/database")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "mode": None, "engine": None, "displayTarget": None}


def test_status_reports_configured_via_env_var(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", "postgresql://user:secret@db.example.com:5432/prism")
    monkeypatch.setenv("LLM_D_BENCH_DB_MODE", "external")
    response = client.get("/api/v1/system/database")
    body = response.json()
    assert body["configured"] is True
    assert body["mode"] == "external"
    assert body["displayTarget"] == "db.example.com:5432/prism"
    assert "secret" not in body["displayTarget"]
    assert "user" not in body["displayTarget"]


def test_status_reports_configured_via_bootstrap_file(monkeypatch):
    from llm_d_bench.db import bootstrap_config

    bootstrap_config.save_bootstrap_config(
        DatabaseBootstrapConfig(mode="external", database_url="postgresql://a:b@127.0.0.1:6543/mydb")
    )
    response = client.get("/api/v1/system/database")
    body = response.json()
    assert body["configured"] is True
    assert body["mode"] == "external"
    assert body["displayTarget"] == "127.0.0.1:6543/mydb"


def test_status_reports_embedded_bootstrap_without_display_target():
    from llm_d_bench.db import bootstrap_config

    bootstrap_config.save_bootstrap_config(DatabaseBootstrapConfig(mode="embedded"))
    response = client.get("/api/v1/system/database")
    body = response.json()
    assert body == {"configured": True, "mode": "embedded", "engine": "postgresql", "displayTarget": None}


def test_post_rejects_external_mode_without_url():
    response = client.post("/api/v1/system/database", json={"mode": "external"})
    assert response.status_code == 422


def test_post_rejects_embedded_mode_with_url():
    response = client.post(
        "/api/v1/system/database",
        json={"mode": "embedded", "databaseUrl": "postgresql://a:b@localhost/db"},
    )
    assert response.status_code == 422


def test_post_rejects_embedded_mode_with_non_postgresql_engine():
    response = client.post("/api/v1/system/database", json={"mode": "embedded", "engine": "mysql"})
    assert response.status_code == 422


def test_post_rejects_engine_mismatched_with_url_scheme():
    response = client.post(
        "/api/v1/system/database",
        json={"mode": "external", "engine": "mysql", "databaseUrl": "postgresql://a:b@localhost:5432/db"},
    )
    assert response.status_code == 422
    assert "does not match" in response.json()["detail"]


def test_post_external_defaults_engine_to_postgresql_when_omitted(monkeypatch):
    monkeypatch.setattr(system_router, "_probe_external_connection", lambda _url: None)
    monkeypatch.setattr(system_router, "_create_tables", lambda: None)
    monkeypatch.setattr(system_router.db_engine, "configure_engine", lambda _settings: None)

    response = client.post(
        "/api/v1/system/database",
        json={"mode": "external", "databaseUrl": "postgresql://a:b@localhost:5432/mydb"},
    )
    assert response.status_code == 200
    assert response.json()["engine"] == "postgresql"


def test_post_external_mysql_success_reports_engine(monkeypatch):
    monkeypatch.setattr(system_router, "_probe_external_connection", lambda _url: None)
    monkeypatch.setattr(system_router, "_create_tables", lambda: None)
    monkeypatch.setattr(system_router.db_engine, "configure_engine", lambda _settings: None)

    response = client.post(
        "/api/v1/system/database",
        json={"mode": "external", "engine": "mysql", "databaseUrl": "mysql://a:b@db.example.com:3306/mydb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "mysql"
    assert body["displayTarget"] == "db.example.com:3306/mydb"

    status = client.get("/api/v1/system/database").json()
    assert status == {
        "configured": True,
        "mode": "external",
        "engine": "mysql",
        "displayTarget": "db.example.com:3306/mydb",
    }


def test_post_surfaces_connection_probe_failure(monkeypatch):
    def _boom(_url: str) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(system_router, "_probe_external_connection", _boom)
    response = client.post(
        "/api/v1/system/database",
        json={"mode": "external", "databaseUrl": "postgresql://a:b@localhost:1/db"},
    )
    assert response.status_code == 422
    assert "connection refused" in response.json()["detail"]


def test_post_external_success_configures_and_persists(monkeypatch):
    monkeypatch.setattr(system_router, "_probe_external_connection", lambda _url: None)
    monkeypatch.setattr(system_router, "_create_tables", lambda: None)
    monkeypatch.setattr(system_router.db_engine, "configure_engine", lambda _settings: None)

    response = client.post(
        "/api/v1/system/database",
        json={"mode": "external", "databaseUrl": "postgresql://a:b@localhost:5432/mydb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["mode"] == "external"
    assert body["displayTarget"] == "localhost:5432/mydb"

    saved = system_router.load_bootstrap_config()
    assert saved is not None
    assert saved.mode == "external"
    assert saved.database_url == "postgresql://a:b@localhost:5432/mydb"


def test_post_embedded_success_configures_and_persists(monkeypatch):
    monkeypatch.setattr(system_router, "_create_tables", lambda: None)
    monkeypatch.setattr(system_router.db_engine, "configure_engine", lambda _settings: None)

    response = client.post("/api/v1/system/database", json={"mode": "embedded"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"configured": True, "mode": "embedded", "engine": "postgresql", "displayTarget": None}

    saved = system_router.load_bootstrap_config()
    assert saved is not None
    assert saved.mode == "embedded"
    assert saved.database_url is None


def test_post_migration_failure_returns_409_and_does_not_persist(monkeypatch):
    monkeypatch.setattr(system_router, "_probe_external_connection", lambda _url: None)
    monkeypatch.setattr(system_router.db_engine, "configure_engine", lambda _settings: None)

    def _boom() -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(system_router, "_create_tables", _boom)

    response = client.post(
        "/api/v1/system/database",
        json={"mode": "external", "databaseUrl": "postgresql://a:b@localhost:5432/mydb"},
    )
    assert response.status_code == 409
    assert system_router.load_bootstrap_config() is None
