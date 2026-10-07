"""Path contract shared by Python, Node and startup tooling."""

import pytest

from llm_d_bench.utils.paths import prism_temp_root, storage_path


def test_lens_roots_ignore_legacy_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LENS_DATA_DIR", raising=False)
    assert storage_path("data", "artifacts") == tmp_path / "xdg/lens/artifacts"
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "lens"))
    assert storage_path("data", "artifacts") == tmp_path / "lens/artifacts"
    monkeypatch.setenv("SIMULATION_TASK_ROOT", str(tmp_path / "old"))
    assert storage_path("data", "artifacts") == tmp_path / "lens/artifacts"


def test_scratch_ignores_legacy_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PRISM_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path / "new"))
    assert prism_temp_root("provider") == tmp_path / "new/provider"


def test_scratch_new_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PRISM_SCRATCH_DIR", raising=False)
    monkeypatch.delenv("LENS_SCRATCH_DIR", raising=False)
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path))
    assert prism_temp_root("provider") == tmp_path / "tmp/provider"


@pytest.mark.parametrize("part", ["../escape", "/escape"])
def test_path_escape_rejected(part):
    with pytest.raises(ValueError):
        storage_path("data", part)


def test_domains_ignore_all_old_storage_variables(tmp_path):
    import os
    import subprocess
    import sys

    old_names = (
        "CONFIGURATION_OUTPUT_DIR",
        "CONFIGURATION_ARTIFACT_DIR",
        "LLM_D_BENCH_DEPLOYMENT_MANIFEST_DIR",
        "LLM_D_BENCH_EVALUATE_STORE",
        "LLM_D_BENCH_EVALUATE_RESULTS_DIR",
        "SIMULATION_TASK_ROOT",
        "TRACE_REPLAY_DATA_DIR",
        "SIMULATION_BACKEND_CACHE_DIR",
        "SIMULATION_TOKENIZER_CACHE_DIR",
        "LLM_D_BENCH_DATA_DIR",
        "PRISM_CLUSTER_SESSION_DIR",
        "MONITORING_OPERATION_ROOT",
        "PRISM_SCRATCH_DIR",
        "LLM_D_BENCH_REPO_CACHE_ROOT",
        "LLM_D_BENCH_KUBESPRAY_CACHE_ROOT",
    )
    environment = {
        **os.environ,
        **{key: str(tmp_path / "old") for key in old_names},
        "LENS_DATA_DIR": str(tmp_path / "data"),
        "LENS_CACHE_DIR": str(tmp_path / "cache"),
        "LENS_SCRATCH_DIR": str(tmp_path / "scratch"),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from llm_d_bench.utils.paths import storage_path, prism_temp_root
from llm_d_bench.configuration.service import CONFIGURATION_OUTPUT_DIR, CONFIGURATION_ARTIFACT_DIR
from llm_d_bench.utils.artifacts import DEPLOYMENT_MANIFEST_DIR
from llm_d_bench.evaluate.router import _results_root, _runs_directory
from llm_d_bench.cluster.settings import ClusterSettings
from llm_d_bench.cluster.repo_downloads import cache_root as repo_root
from llm_d_bench.cluster.bootstrap.kubespray import cache_root as kubespray_root
from llm_d_bench.simulation.service import task_root
from llm_d_bench.simulation.traces import BaseTrace
assert CONFIGURATION_OUTPUT_DIR == storage_path("data", "artifacts", "configurations")
assert CONFIGURATION_ARTIFACT_DIR == storage_path("data", "metadata", "configurations")
assert DEPLOYMENT_MANIFEST_DIR == storage_path("data", "artifacts", "deployment-manifests")
assert _results_root == storage_path("data", "artifacts", "evaluations")
assert _runs_directory == storage_path("data", "metadata", "evaluations")
assert task_root() == storage_path("data", "artifacts", "simulations")
assert BaseTrace.trace_root() == storage_path("data", "datasets")
assert ClusterSettings.from_environment().session_directory == storage_path(
    "data", "credentials", "clusters", "cluster_sessions"
)
assert repo_root() == storage_path("cache", "repos")
assert kubespray_root() == storage_path("cache", "kubespray")
assert prism_temp_root("test") == storage_path("scratch", "test")
""",
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (tmp_path / "old").exists()
