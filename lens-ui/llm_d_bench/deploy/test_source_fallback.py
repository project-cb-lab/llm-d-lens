from types import SimpleNamespace

import pytest

from llm_d_bench.cluster import registry
from llm_d_bench.deploy.application import cluster_deployment_source


@pytest.fixture
def cluster(monkeypatch):
    value = SimpleNamespace(id="cluster-1", name="wenxin-test", llm_d_repo_path=None, llm_d_ref="requested-version")
    monkeypatch.setattr(registry, "require_cluster", lambda _: value)
    monkeypatch.delenv("LLM_D_ROOT", raising=False)
    return value


def test_missing_source_rejects_configured_backup(cluster, monkeypatch, tmp_path):
    (tmp_path / "guides").mkdir()
    monkeypatch.setenv("LLM_D_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="Software Versions"):
        cluster_deployment_source(SimpleNamespace(server_id=cluster.id))


def test_registered_source_wins_over_backup(cluster, monkeypatch, tmp_path):
    primary = tmp_path / "primary"
    (primary / "guides").mkdir(parents=True)
    cluster.llm_d_repo_path = str(primary)
    monkeypatch.setenv("LLM_D_ROOT", str(tmp_path))
    result = cluster_deployment_source(SimpleNamespace(server_id=cluster.id))
    assert result["resolved_from"] == "cluster-registry"
    assert result["resolved_repository"] == str(primary)
    assert "warning" not in result


def test_unavailable_registered_source_rejects_backup(cluster, monkeypatch, tmp_path):
    cluster.llm_d_repo_path = str(tmp_path / "missing")
    (tmp_path / "guides").mkdir()
    monkeypatch.setenv("LLM_D_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="Software Versions"):
        cluster_deployment_source(SimpleNamespace(server_id=cluster.id))


def test_invalid_backup_and_request_supplied_path_do_not_bypass_validation(cluster, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_D_ROOT", str(tmp_path / "missing"))
    (tmp_path / "guides").mkdir()
    with pytest.raises(ValueError, match="Software Versions"):
        cluster_deployment_source(SimpleNamespace(server_id=cluster.id), {"resolved_repository": str(tmp_path)})


def test_benchmark_rejects_unregistered_managed_cache(cluster, monkeypatch, tmp_path):
    from llm_d_bench.cluster.deployment_source import resolve_cluster_benchmark_source

    cluster.llm_d_benchmark_repo_path = None
    cluster.llm_d_benchmark_ref = None
    monkeypatch.delenv("LLM_D_BENCHMARK_ROOT", raising=False)
    monkeypatch.setenv("LLM_D_BENCHMARK_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_D_BENCHMARK_REVISION", "cached-ref")
    checkout = tmp_path / "llmdbenchmark" / "cached-ref"
    checkout.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname="llm-d-benchmark"\n')
    with pytest.raises(ValueError, match="Software Versions"):
        resolve_cluster_benchmark_source(cluster)


def test_benchmark_backup_missing_remains_actionable(cluster, monkeypatch, tmp_path):
    from llm_d_bench.cluster.deployment_source import resolve_cluster_benchmark_source

    cluster.llm_d_benchmark_repo_path = None
    monkeypatch.setenv("LLM_D_BENCHMARK_ROOT", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="Software Versions"):
        resolve_cluster_benchmark_source(cluster)
