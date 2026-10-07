import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from llm_d_bench.deploy.providers.guide_adapter import GuideDeploymentArtifact
from llm_d_bench.evaluate.request_evidence import kv_working_set
from llm_d_bench.monitoring.kv_trace import engine
from llm_d_bench.monitoring.kv_trace.collector import KVTraceCollector, assemble, client_request_ids
from llm_d_bench.monitoring.kv_trace.manifest import LABEL, instrument_manifest


def manager_class():
    class Manager:
        def __init__(self):
            spec = type("FullAttentionSpec", (), {"block_size": 2})()
            self.kv_cache_config = SimpleNamespace(kv_cache_groups=[SimpleNamespace(kv_cache_spec=spec)])
            self.enable_caching = True
            self.block_size = 2
            self.freed = []

        def get_blocks(self, request_id):
            return SimpleNamespace(
                blocks=(
                    [
                        SimpleNamespace(block_hash=123, is_null=False),
                        SimpleNamespace(block_hash=456, is_null=False),
                    ],
                )
            )

        def free(self, request):
            self.freed.append(request.request_id)
            return "freed"

    return Manager


def request(identity="cmpl-one-0", tokens=None, status="FINISHED_STOPPED"):
    return SimpleNamespace(
        request_id=identity,
        prompt_token_ids=tokens or [1, 2, 3, 4, 5],
        num_computed_tokens=6,
        status=SimpleNamespace(name=status),
    )


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "ROOT", tmp_path / "trace")
    monkeypatch.setenv("PRISM_KV_TRACE_NAMESPACE", "fixed-model-cache")
    cls = manager_class()
    engine.install(SimpleNamespace(KVCacheManager=cls))
    return cls()


def test_probe_records_engine_blocks_and_collector_deduplicates_revisits(probe):
    engine.command("begin", "test")
    assert probe.free(request()) == "freed"
    probe.free(request("cmpl-two-0"))
    probe.free(request("health-check"))
    snapshot = engine.command("end", "test")
    trace = assemble([({"name": "model", "uid": "pod-1"}, snapshot)], 2, client_ids={"cmpl-one", "cmpl-two"})
    result = kv_working_set(trace)
    assert result["status"] == "measured"
    assert result["value"] == 4  # Two full blocks; partial prompt tail is excluded.
    assert result["unique_blocks"] == 2
    assert result["measurement_scope"] == "completed-full-prompt-blocks"
    assert "prompt_token_ids" not in json.dumps(snapshot)
    assert len(probe.freed) == 3
    assert not list(engine.ROOT.glob("*.jsonl"))


def test_active_session_cannot_be_overwritten_and_preemption_not_completed(probe):
    engine.command("begin", "first")
    with pytest.raises(ValueError, match="another"):
        engine.command("begin", "second")
    with pytest.raises(ValueError, match="mismatch"):
        engine.command("end", "second")
    probe.free(request(status="RUNNING"))
    probe.free(request(status="FINISHED_ABORTED"))
    assert engine.command("end", "first")["requests"] == []


def test_unsupported_request_does_not_break_inference_or_claim_measurement(probe):
    engine.command("begin", "test")
    bad = request()
    bad.lora_request = object()
    assert probe.free(bad) == "freed"
    snapshot = engine.command("end", "test")
    trace = assemble([({"name": "model", "uid": "p"}, snapshot)], 1, client_ids={"cmpl-one"})
    assert not trace["complete"]
    assert "LoRA" in trace["reason"]


def test_completion_coverage_and_engine_restart_are_checked(probe):
    engine.command("begin", "test")
    probe.free(request())
    snapshot = engine.command("end", "test")
    snapshots = [({"name": "model", "uid": "p"}, snapshot)]
    assert not assemble(snapshots, 2, client_ids={"cmpl-one", "missing"})["complete"]
    assert not assemble(snapshots, 1, False, {"cmpl-one"})["complete"]
    snapshot["engines_end"] = []
    assert "processes changed" in assemble(snapshots, 1, client_ids={"cmpl-one"})["reason"]


@pytest.mark.parametrize("identity", ["cmpl-one-0-a1b2c3d4", "cmpl-one-a1b2c3d4"])
def test_vllm_randomized_internal_request_id_correlates(probe, identity):
    engine.command("begin", "test")
    probe.free(request(identity))
    snapshot = engine.command("end", "test")
    trace = assemble([({"name": "model", "uid": "p"}, snapshot)], 1, client_ids={"cmpl-one"})
    assert trace["complete"]
    assert trace["request_count"] == 1


def test_randomized_id_does_not_accept_ambiguous_client_mapping(probe):
    engine.command("begin", "test")
    probe.free(request("cmpl-one-0-a1b2c3d4"))
    snapshot = engine.command("end", "test")
    trace = assemble([({"name": "model", "uid": "p"}, snapshot)], 2, client_ids={"cmpl-one", "cmpl-one-0-a1b2c3d4"})
    assert not trace["complete"]
    assert "Ambiguous" in trace["reason"]


def test_manifest_injection_preserves_original_and_non_model_containers(tmp_path):
    original = tmp_path / "original.yaml"
    docs = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "vllm",
                                "image": "vllm:v1",
                                "args": ["serve", "model"],
                                "env": [{"name": "PYTHONPATH", "value": "/existing"}],
                            },
                            {"name": "router", "image": "epp:v1"},
                        ]
                    }
                }
            },
        }
    ]
    original.write_text(yaml.safe_dump_all(docs))
    artifact = GuideDeploymentArtifact("tiered-prefix-cache", "original", manifest_ref=str(original))
    patched = instrument_manifest(artifact, tmp_path / "out")
    resources = list(yaml.safe_load_all(Path(patched.manifest_ref).read_text()))
    assert resources[0]["kind"] == "ConfigMap"
    assert "sitecustomize.py" in resources[0]["data"]
    pod = resources[1]["spec"]["template"]
    assert pod["metadata"]["labels"][LABEL] == "v1"
    assert pod["spec"]["containers"][0]["env"][0]["value"].endswith(":/existing")
    assert "env" not in pod["spec"]["containers"][1]
    assert yaml.safe_load(original.read_text()) == docs[0]
    assert patched.deployment_contract == {}  # Do not change legacy lifecycle dispatch.


def client_artifacts(root, identities):
    root.mkdir(parents=True, exist_ok=True)
    summary = root / "summary_lifecycle_metrics.json"
    summary.write_text(json.dumps({"successes": {"count": len(identities)}}))
    (root / "stage_0_lifecycle_metrics.json").write_text(summary.read_text())
    (root / "per_request_lifecycle_metrics.json").write_text(
        json.dumps(
            [
                {"error": None, "info": {"response_metrics": {"response_chunks": [json.dumps({"id": identity})]}}}
                for identity in identities
            ]
        )
    )
    return {"summary_path": str(summary), "success_count": len(identities)}


@pytest.mark.asyncio
async def test_automatic_begin_finish_writes_ingestible_single_stage_trace(probe, tmp_path):
    pod = {
        "metadata": {"name": "model", "uid": "p", "labels": {LABEL: "v1"}},
        "spec": {"containers": [{"name": "vllm", "env": [{"name": "PRISM_KV_TRACE_NAMESPACE", "value": "x"}]}]},
        "status": {"containerStatuses": [{"name": "vllm", "ready": True, "restartCount": 0}]},
    }

    async def command(*args):
        if args[0] == "get":
            return {"items": [pod]}
        return engine.command(args[-2], args[-1])

    collector = KVTraceCollector("ns", {}, True, command)
    metrics = client_artifacts(tmp_path / "results", ["cmpl-one"])
    await collector.begin(tmp_path / "results")
    probe.free(request())
    await collector.finish(tmp_path / "results", metrics, True)
    trace = json.loads((tmp_path / "results" / "stage_0_kv_access.json").read_text())
    assert kv_working_set(trace, 0)["value"] == 4
    assert collector.session is None
    assert not (engine.ROOT / "active.json").exists()


@pytest.mark.asyncio
async def test_cancellation_or_failure_closes_probe_without_measured_result(probe, tmp_path):
    collector = KVTraceCollector("ns", {}, True)

    async def discover():
        return [{"name": "model", "uid": "p"}]

    async def remote(pod, action):
        return engine.command(action, collector.session)

    collector.discover = discover
    collector.probe = remote
    metrics = client_artifacts(tmp_path / "results", ["cmpl-one"])
    await collector.begin(tmp_path / "results")
    probe.free(request())
    await collector.finish(tmp_path / "results", metrics, False)
    trace = json.loads((tmp_path / "results" / "summary_kv_access.json").read_text())
    assert not trace["complete"]
    assert not (engine.ROOT / "active.json").exists()


def test_client_ids_reject_missing_or_duplicate_correlations(tmp_path):
    metrics = client_artifacts(tmp_path, ["duplicate", "duplicate"])
    with pytest.raises(ValueError, match="Unique"):
        client_request_ids(metrics["summary_path"])


def test_python_startup_installs_probe_in_imported_vllm_process(tmp_path):
    import os
    import subprocess
    import sys

    source = Path(engine.__file__).parent
    (tmp_path / "sitecustomize.py").write_text((source / "sitecustomize.py").read_text())
    (tmp_path / "prism_kv_engine.py").write_text((source / "engine.py").read_text())
    package = tmp_path / "vllm" / "v1" / "core"
    package.mkdir(parents=True)
    for directory in [package, package.parent, package.parent.parent]:
        (directory / "__init__.py").write_text("")
    (package / "kv_cache_manager.py").write_text(
        "class KVCacheManager:\n def __init__(self): pass\n def free(self, request): pass\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from vllm.v1.core.kv_cache_manager import KVCacheManager; print(KVCacheManager._prism_kv_trace)",
        ],
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PRISM_KV_TRACE_NAMESPACE": "model"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["evaluation", "existing-endpoint"])
async def test_evaluate_matrix_automatically_records_and_persists_engine_working_set(
    probe, monkeypatch, tmp_path, ownership
):
    from llm_d_bench.evaluate.test_matrix import _FakeProcess, _patch_execute_dependencies, router

    run = {
        "id": "automatic-kv",
        "deployment_execution_id": "exec-1",
        "deployment_ownership": ownership,
        "specification_file": "guides/tiered-prefix-cache",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 128, "osl": 64}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 1}],
        "warmup_requests": 0,
    }

    class Process(_FakeProcess):
        async def communicate(self):
            probe.free(request())
            root = tmp_path / run["id"] / "matrix-0"
            client_artifacts(root, ["cmpl-one"])
            return b"", b""

    _patch_execute_dependencies(monkeypatch, tmp_path, run, [Process(0)])

    async def discover(self):
        return [{"name": "model", "uid": "p"}]

    async def remote(self, pod, action):
        return engine.command(action, self.session)

    monkeypatch.setattr(KVTraceCollector, "discover", discover)
    monkeypatch.setattr(KVTraceCollector, "probe", remote)
    await router._execute(run["id"])
    assert run["status"] == "succeeded", run.get("error")
    point = run["matrix_results"][0]
    assert point["metrics"]["distinct_kv_working_set"]["value"] == 4
    assert point["stage_metrics"][0]["metrics"]["distinct_kv_working_set"]["value"] == 4
    assert not (engine.ROOT / "active.json").exists()


@pytest.mark.asyncio
async def test_unsupported_worker_scope_does_not_touch_cluster(tmp_path):
    async def unexpected(*args):
        raise AssertionError("must not access cluster")

    collector = KVTraceCollector("ns", {}, True, unexpected, unsupported_reason="parallelism=1 required")
    metrics = client_artifacts(tmp_path, ["cmpl-one"])
    await collector.begin(tmp_path)
    await collector.finish(tmp_path, metrics, True)
    trace = json.loads((tmp_path / "summary_kv_access.json").read_text())
    assert trace["reason"] == "parallelism=1 required"
    assert kv_working_set(trace)["status"] == "unavailable"
