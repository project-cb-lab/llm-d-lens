"""Tests for llm-d / llm-d-benchmark repo downloads (wizard Software Versions step)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.cluster import registry, repo_downloads, service
from llm_d_bench.cluster.settings import ClusterSettings
from llm_d_bench.utils.shell import CommandResult

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    yield


@pytest.fixture(autouse=True)
def _reset_status():
    repo_downloads._status.clear()
    yield
    repo_downloads._status.clear()


def _fake_git_success(argv, timeout=None):
    # Simulate `git clone --branch ref ...` succeeding by creating a `.git` dir
    # at the destination (the real clone/checkout subprocess calls are never
    # invoked in tests).
    if argv[:2] == ["git", "clone"]:
        dest = argv[-1]
        from pathlib import Path

        Path(dest, ".git").mkdir(parents=True, exist_ok=True)
        return CommandResult(tuple(argv), 0, "", "")
    return CommandResult(tuple(argv), 0, "", "")


def test_repo_dir_is_stable_and_sanitizes_ref():
    path_a = repo_downloads.repo_dir("llm-d", "feature/foo")
    path_b = repo_downloads.repo_dir("llm-d", "feature/foo")
    assert path_a == path_b
    assert "/" not in path_a.name


@pytest.mark.asyncio
async def test_ensure_downloaded_clones_when_not_cached(monkeypatch):
    monkeypatch.setattr(repo_downloads, "run_sync", _fake_git_success)
    path = await repo_downloads.ensure_downloaded("cluster-1", "llm-d", "v0.2.0")
    assert path.name == "v0.2.0"
    assert (path / ".git").exists()
    status = repo_downloads.get_status("cluster-1", "llm-d")
    assert status.state == "ready"
    assert status.path == str(path)


@pytest.mark.asyncio
async def test_ensure_downloaded_reuses_existing_cache(monkeypatch):
    calls = []

    def _tracking(argv, timeout=None):
        calls.append(argv)
        return _fake_git_success(argv, timeout)

    monkeypatch.setattr(repo_downloads, "run_sync", _tracking)
    await repo_downloads.ensure_downloaded("cluster-1", "llm-d", "v0.2.0")
    assert len(calls) == 1
    await repo_downloads.ensure_downloaded("cluster-1", "llm-d", "v0.2.0")
    assert len(calls) == 1  # second call is a cache hit, no further git invocations


@pytest.mark.asyncio
async def test_ensure_downloaded_records_failure(monkeypatch):
    def _fail(argv, timeout=None):
        return CommandResult(tuple(argv), 1, "", "not found")

    monkeypatch.setattr(repo_downloads, "run_sync", _fail)
    with pytest.raises(RuntimeError):
        await repo_downloads.ensure_downloaded("cluster-1", "llm-d", "bogus-ref")
    status = repo_downloads.get_status("cluster-1", "llm-d")
    assert status.state == "failed"
    assert status.error


def test_registry_clears_stale_repo_path_when_ref_changes_without_new_path(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("c1", "", "kubeconfig: {}", llm_d_ref="v0.2.0")
    repo_path = str(tmp_path / "repo" / "v0.2.0")
    with_path = registry.update_cluster(cluster.id, llm_d_ref="v0.2.0", llm_d_repo_path=repo_path)
    assert with_path.llm_d_repo_path == repo_path

    # Changing the ref alone (no accompanying path) must drop the stale path.
    changed_ref = registry.update_cluster(with_path.id, llm_d_ref="main")
    assert changed_ref.llm_d_ref == "main"
    assert changed_ref.llm_d_repo_path is None


def test_service_record_repo_download_persists_path(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("c1", "", "kubeconfig: {}")
    updated = service.record_repo_download(cluster.id, llm_d_ref="v0.3.0", llm_d_repo_path="/cache/llm-d/v0.3.0")
    assert updated.llm_d_ref == "v0.3.0"
    assert updated.llm_d_repo_path == "/cache/llm-d/v0.3.0"


def test_software_downloads_endpoints_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("c1", "", "kubeconfig: {}")

    async def _fake_ensure_downloaded(cluster_id, repo, ref):
        repo_downloads._set_status(cluster_id, repo, ref=ref, state="ready", path=f"/cache/{repo}/{ref}", error=None)
        from pathlib import Path

        return Path(f"/cache/{repo}/{ref}")

    monkeypatch.setattr(repo_downloads, "ensure_downloaded", _fake_ensure_downloaded)

    response = client.post(
        f"/api/cluster/clusters/{cluster.id}/software-downloads",
        json={"llmDRef": "v0.2.0", "llmDBenchmarkRef": "main"},
    )
    assert response.status_code == 202

    # Downloads run as fire-and-forget background tasks; give them a tick.
    asyncio.run(asyncio.sleep(0.05))

    status_response = client.get(f"/api/cluster/clusters/{cluster.id}/software-downloads")
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["llmD"]["state"] == "ready"
    assert body["llmDBenchmark"]["state"] == "ready"

    # The request body is ignored: downloads always follow the Lens stack profile.
    from llm_d_bench import versions

    updated_cluster = registry.get_cluster(cluster.id)
    assert updated_cluster.llm_d_repo_path == f"/cache/llm-d/{versions.stack().llm_d}"
    assert updated_cluster.llm_d_benchmark_repo_path == f"/cache/llm-d-benchmark/{versions.stack().llm_d_benchmark}"
