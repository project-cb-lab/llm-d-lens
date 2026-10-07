"""Tests for llm-d repository checkout resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_d_bench.monitoring.cluster_stack import repo


@pytest.fixture(autouse=True)
def _reset_repo_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_D_REPO_ROOT", raising=False)
    yield


def _write_repo(root: Path) -> None:
    installer = root / "guides/recipes/observability/install-prometheus-grafana.sh"
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.write_text("#!/bin/sh\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_repo_root_rejects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    checkout = tmp_path / "checkout"
    _write_repo(checkout)
    monkeypatch.setenv("LLM_D_REPO_ROOT", str(checkout))

    assert await repo.repo_root() is None


@pytest.mark.asyncio
async def test_repo_root_rejects_unregistered_cached_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_D_REPO_CACHE_ROOT", str(tmp_path))
    _write_repo(tmp_path / "llm-d")
    assert await repo.repo_root() is None
    assert (tmp_path / "llm-d").is_dir()


@pytest.mark.asyncio
async def test_repo_root_does_not_download_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_D_REPO_CACHE_ROOT", str(tmp_path))
    assert await repo.repo_root() is None
    assert not (tmp_path / "llm-d").exists()


@pytest.mark.asyncio
async def test_repo_root_prefers_cluster_repo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A cluster's own pinned llm-d checkout (from the wizard/edit "llm-d
    versions" step) must win over LLM_D_REPO_ROOT, so the monitoring
    installer always runs against the version the operator chose for that
    specific cluster."""
    cluster_checkout = tmp_path / "cluster-checkout"
    _write_repo(cluster_checkout)
    env_checkout = tmp_path / "env-checkout"
    _write_repo(env_checkout)
    monkeypatch.setenv("LLM_D_REPO_ROOT", str(env_checkout))

    import llm_d_bench.cluster.registry as cluster_registry

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster",
        lambda cluster_id: (
            SimpleNamespace(llm_d_repo_path=str(cluster_checkout)) if cluster_id == "cluster-1" else None
        ),
    )

    assert await repo.repo_root(cluster_id="cluster-1") == cluster_checkout.resolve()
    # Missing or unknown clusters must never use the environment checkout.
    assert await repo.repo_root() is None
    assert await repo.repo_root(cluster_id="unknown") is None


@pytest.mark.asyncio
async def test_repo_root_unavailable_when_cluster_has_no_downloaded_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env_checkout = tmp_path / "env-checkout"
    _write_repo(env_checkout)
    monkeypatch.setenv("LLM_D_REPO_ROOT", str(env_checkout))

    import llm_d_bench.cluster.registry as cluster_registry

    monkeypatch.setattr(cluster_registry, "get_cluster", lambda cluster_id: SimpleNamespace(llm_d_repo_path=None))

    assert await repo.repo_root(cluster_id="cluster-1") is None


@pytest.mark.asyncio
async def test_repo_root_ignores_cluster_path_that_is_not_a_valid_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A stale/incomplete recorded path (e.g. cleared mid-download) must not
    be treated as usable or fall back to an unrelated checkout instead of a
    directory missing the observability recipe."""
    env_checkout = tmp_path / "env-checkout"
    _write_repo(env_checkout)
    monkeypatch.setenv("LLM_D_REPO_ROOT", str(env_checkout))
    bogus = tmp_path / "not-a-repo"
    bogus.mkdir()

    import llm_d_bench.cluster.registry as cluster_registry

    monkeypatch.setattr(cluster_registry, "get_cluster", lambda cluster_id: SimpleNamespace(llm_d_repo_path=str(bogus)))

    assert await repo.repo_root(cluster_id="cluster-1") is None


@pytest.mark.asyncio
async def test_repo_root_returns_none_without_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_D_REPO_CACHE_ROOT", str(tmp_path))
    assert await repo.repo_root() is None
