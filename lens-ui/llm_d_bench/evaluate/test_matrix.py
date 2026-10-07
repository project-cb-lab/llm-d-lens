"""Tests for the ISL x OSL x concurrency matrix sweep and its comparison statistics."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from llm_d_bench.evaluate.models import BenchmarkSpec, ConcurrencyStage, WorkloadMatrixPoint

router = importlib.import_module("llm_d_bench.evaluate.router")


def test_workload_matrix_point_and_concurrency_stage_validate_bounds():
    with pytest.raises(ValidationError):
        WorkloadMatrixPoint(isl=0, osl=128)
    with pytest.raises(ValidationError):
        ConcurrencyStage(concurrency=1, num_requests=0)
    point = WorkloadMatrixPoint(isl=1024, osl=128)
    stage = ConcurrencyStage(concurrency=8, num_requests=96)
    assert point.isl == 1024 and point.osl == 128
    assert stage.concurrency == 8 and stage.num_requests == 96


def test_inline_workload_yaml_is_validated_and_mutually_exclusive_with_matrix():
    valid = """load: {type: concurrent, stages: [{concurrency_level: 1, num_requests: 2}]}
api: {type: completion, streaming: true}
data: {type: random, input_distribution: {min: 8, max: 8, mean: 8}, output_distribution: {min: 4, max: 4, mean: 4}}
"""
    request = router.EvaluateRunRequest(
        deployment_execution_id="00000000-0000-4000-8000-000000000001",
        workload_yaml=valid,
    )
    assert request.workload_yaml == valid
    with pytest.raises(ValidationError, match="mutually exclusive"):
        router.EvaluateRunRequest(
            deployment_execution_id="00000000-0000-4000-8000-000000000001",
            workload_yaml=valid,
            matrix=[{"isl": 8, "osl": 4}],
        )
    with pytest.raises(ValidationError, match="requires a data mapping"):
        router.EvaluateRunRequest(
            deployment_execution_id="00000000-0000-4000-8000-000000000001",
            workload_yaml="load: {}\napi: {}\n",
        )


def test_inline_workload_yaml_is_bound_to_selected_deployment():
    content = """load: {type: constant, stages: [{rate: 1, duration: 30}]}
api: {type: completion, streaming: true}
data: {type: random}
server: {type: vllm, model_name: stale, base_url: 'http://stale.invalid'}
"""

    parsed = yaml.safe_load(
        router._inline_workload_yaml(content, "Qwen/Qwen3-0.6B", "http://optimized-baseline-epp:80")
    )

    assert parsed["server"] == {
        "type": "vllm",
        "model_name": "Qwen/Qwen3-0.6B",
        "base_url": "http://optimized-baseline-epp:80",
        "ignore_eos": True,
    }


def test_workload_yaml_binds_gateway_api_key_when_routed():
    content = (
        "load: {type: constant, stages: [{rate: 1, duration: 30}]}\n"
        "api: {type: completion, streaming: true}\n"
        "data: {type: random}\n"
    )
    parsed = yaml.safe_load(
        router._inline_workload_yaml(
            content, "Qwen/Qwen3-0.6B", "http://gateway:30012", api_key="lens-mk-abc"
        )
    )
    assert parsed["server"]["api_key"] == "lens-mk-abc"
    assert parsed["server"]["base_url"] == "http://gateway:30012"


@pytest.mark.parametrize("request_type", [BenchmarkSpec, router.EvaluateRunRequest])
@pytest.mark.parametrize("load_type", ["constant", "poisson", "concurrent"])
@pytest.mark.parametrize("schedule", [{}, {"stages": []}, {"stages": [], "sweep": {}}])
def test_inline_workload_rejects_missing_load_schedule(request_type, load_type, schedule):
    content = yaml.safe_dump(
        {
            "load": {"type": load_type, **schedule},
            "api": {"type": "completion"},
            "data": {"type": "random"},
        }
    )
    with pytest.raises(ValidationError, match="requires non-empty load.stages or load.sweep"):
        request_type(deployment_execution_id="deployment", workload_yaml=content)


@pytest.mark.parametrize("request_type", [BenchmarkSpec, router.EvaluateRunRequest])
@pytest.mark.parametrize(
    "load",
    [
        {"type": "constant", "stages": [{"rate": 1, "duration": 30}]},
        {"type": "poisson", "sweep": {"type": "linear", "start": 1, "stop": 3, "step": 1, "duration": 30}},
        {"type": "concurrent", "stages": [{"concurrency_level": 1, "num_requests": 2}]},
    ],
)
def test_inline_workload_accepts_load_schedule(request_type, load):
    content = yaml.safe_dump({"load": load, "api": {"type": "completion"}, "data": {"type": "random"}})
    assert request_type(deployment_execution_id="deployment", workload_yaml=content).workload_yaml == content


@pytest.mark.parametrize(
    "schedule",
    [
        {"stages": "invalid"},
        {"stages": [1]},
        {"stages": [{}]},
        {"sweep": "invalid"},
    ],
)
def test_inline_workload_rejects_malformed_schedule(schedule):
    content = yaml.safe_dump({"load": {"type": "constant", **schedule}, "api": {}, "data": {}})
    with pytest.raises(ValidationError, match="workload_yaml load"):
        BenchmarkSpec(workload_yaml=content)


def test_inline_workload_binding_rejects_legacy_incomplete_template():
    with pytest.raises(ValueError, match="requires non-empty load.stages or load.sweep"):
        router._inline_workload_yaml(
            "load: {type: constant}\napi: {type: completion}\ndata: {type: random}\n",
            "Qwen/Qwen3-8B",
            "http://endpoint.local",
        )


@pytest.mark.asyncio
async def test_saved_incomplete_workload_fails_before_deployment_access(monkeypatch):
    run = {"workload_yaml": "load: {type: constant}\napi: {}\ndata: {}\n"}
    monkeypatch.setattr(router, "_get", lambda *_: run)
    monkeypatch.setattr(router, "_save", lambda _: None)

    def unexpected_deployment_access(*_):
        pytest.fail("invalid workload must fail before accessing a deployment")

    monkeypatch.setattr(router._store, "get_execution", unexpected_deployment_access)
    await router._execute("incomplete-workload")
    assert run["status"] == "failed"
    assert "requires non-empty load.stages or load.sweep" in run["error"]


def test_matrix_workload_yaml_is_exact_length_and_closed_loop():
    point = WorkloadMatrixPoint(isl=1024, osl=128)
    stages = [ConcurrencyStage(concurrency=1, num_requests=32), ConcurrencyStage(concurrency=8, num_requests=96)]

    content = router._matrix_workload_yaml(point, stages, "Qwen/Qwen3-32B", "http://endpoint.local")
    parsed = yaml.safe_load(content)

    assert parsed["load"] == {
        "type": "concurrent",
        "stages": [
            {"concurrency_level": 1, "num_requests": 32},
            {"concurrency_level": 8, "num_requests": 96},
        ],
    }
    assert parsed["server"]["ignore_eos"] is True
    assert parsed["server"]["model_name"] == "Qwen/Qwen3-32B"
    assert parsed["server"]["base_url"] == "http://endpoint.local"
    # Exact-length: min == max == mean and no spread, matching the whitepaper's methodology.
    assert parsed["data"]["input_distribution"] == {"min": 1024, "max": 1024, "mean": 1024, "std_dev": 0}
    assert parsed["data"]["output_distribution"] == {"min": 128, "max": 128, "mean": 128, "std_dev": 0}
    assert parsed["report"]["request_lifecycle"]["per_stage"] is True


def _write_stage_metrics(
    directory, stage_index, *, output_tokens_per_sec, ttft_mean_s, ttft_p99_s, total_output_tokens, duration_s
):
    payload = {
        "benchmark_time_seconds": duration_s,
        "load_summary": {"count": 10},
        "successes": {
            "count": 10,
            "prompt_tokens": {"total": 10240},
            "output_tokens": {"total": total_output_tokens},
            "throughput": {
                "output_tokens_per_sec": output_tokens_per_sec,
                "input_tokens_per_sec": 100,
                "requests_per_sec": 1,
            },
            "latency": {
                "time_to_first_token": {"mean": ttft_mean_s, "median": ttft_mean_s, "p99": ttft_p99_s},
                "time_per_output_token": {"mean": 0.05, "median": 0.05, "p99": 0.06},
                "request_latency": {"mean": 1.5, "median": 1.5, "p99": 1.8},
                "inter_token_latency": {"mean": 0.05, "median": 0.05, "p99": 0.06},
                "normalized_time_per_output_token": {"mean": 0.05, "median": 0.05, "p99": 0.06},
            },
        },
        "failures": {"count": 0},
    }
    (directory / f"stage_{stage_index}_lifecycle_metrics.json").write_text(json.dumps(payload), encoding="utf-8")
    # The overall (all-stages) summary file is required by _metric_summary too; reuse the
    # single stage's payload since these tests only exercise one stage per point.
    (directory / "summary_lifecycle_metrics.json").write_text(json.dumps(payload), encoding="utf-8")


def test_metric_summary_reads_a_specific_stage_file(tmp_path):
    _write_stage_metrics(
        tmp_path,
        0,
        output_tokens_per_sec=150.0,
        ttft_mean_s=1.0,
        ttft_p99_s=2.0,
        total_output_tokens=1280,
        duration_s=10.0,
    )

    stage_metrics = router._stage_metric_summary(tmp_path, 0)

    assert stage_metrics["throughput_tps"] == 150.0
    assert stage_metrics["latency_distributions"]["ttft"]["mean_ms"] == 1000.0
    assert stage_metrics["latency_distributions"]["ttft"]["p99_ms"] == 2000.0
    assert router._stage_metric_summary(tmp_path, 1) == {}


def _matrix_point(tmp_path, name, isl, osl, *, throughput, ttft_mean_s, ttft_p99_s, total_output_tokens, duration_s):
    point_dir = tmp_path / name
    point_dir.mkdir()
    _write_stage_metrics(
        point_dir,
        0,
        output_tokens_per_sec=throughput,
        ttft_mean_s=ttft_mean_s,
        ttft_p99_s=ttft_p99_s,
        total_output_tokens=total_output_tokens,
        duration_s=duration_s,
    )
    metrics = router._metric_summary(point_dir)
    stage_metrics = [{"concurrency": 8, "num_requests": 96, "metrics": router._stage_metric_summary(point_dir, 0)}]
    return {"isl": isl, "osl": osl, "status": "succeeded", "metrics": metrics, "stage_metrics": stage_metrics}


def test_matrix_comparison_computes_geometric_mean_and_suite_normalized_throughput(tmp_path):
    # Guide: 2x the baseline's output throughput and half the baseline's mean TTFT, in every case.
    guide_results = [
        _matrix_point(
            tmp_path,
            "guide-0",
            1024,
            128,
            throughput=200.0,
            ttft_mean_s=0.5,
            ttft_p99_s=0.6,
            total_output_tokens=2000,
            duration_s=10.0,
        ),
        _matrix_point(
            tmp_path,
            "guide-1",
            8192,
            128,
            throughput=100.0,
            ttft_mean_s=1.0,
            ttft_p99_s=1.2,
            total_output_tokens=1000,
            duration_s=10.0,
        ),
    ]
    baseline_results = [
        _matrix_point(
            tmp_path,
            "baseline-0",
            1024,
            128,
            throughput=100.0,
            ttft_mean_s=1.0,
            ttft_p99_s=1.2,
            total_output_tokens=1000,
            duration_s=10.0,
        ),
        _matrix_point(
            tmp_path,
            "baseline-1",
            8192,
            128,
            throughput=50.0,
            ttft_mean_s=2.0,
            ttft_p99_s=2.4,
            total_output_tokens=500,
            duration_s=10.0,
        ),
    ]

    statistics = router._matrix_comparison(guide_results, baseline_results)

    assert statistics["case_count"] == 2
    assert statistics["geometric_mean_ratio"]["throughput_tps"] == pytest.approx(2.0)
    assert statistics["geometric_mean_ratio"]["ttft_ms"] == pytest.approx(0.5)
    assert statistics["geometric_mean_ratio_p99"]["ttft_ms"] == pytest.approx(0.5)
    # Suite-normalized throughput: guide totals (3000 tokens / 20s) vs baseline (1500 tokens / 20s) = 2.0.
    assert statistics["suite_normalized_throughput_ratio"] == pytest.approx(2.0)
    assert statistics["grouped_ratio"]["isl"]["1024"]["throughput_tps"] == pytest.approx(2.0)
    assert statistics["grouped_ratio"]["osl"]["128"]["throughput_tps"] == pytest.approx(2.0)
    assert statistics["grouped_ratio"]["concurrency"]["8"]["throughput_tps"] == pytest.approx(2.0)


def test_matrix_comparison_returns_none_when_no_points_match():
    guide_results = [{"isl": 1024, "osl": 128, "status": "failed"}]
    baseline_results = [{"isl": 1024, "osl": 128, "status": "succeeded", "stage_metrics": []}]

    assert router._matrix_comparison(guide_results, baseline_results) is None


def test_comparison_report_adds_pairwise_guide_topology_comparisons(monkeypatch):
    cases = [
        {
            "id": "guide-1",
            "kind": "guide",
            "workload_ids": ["one-p-one-d"],
            "metrics": {"throughput_tps": 200.0, "ttft_ms": 50.0},
            "baseline_case_ids": [],
        },
        {
            "id": "guide-2",
            "kind": "guide",
            "workload_ids": ["two-p-two-d"],
            "metrics": {"throughput_tps": 100.0, "ttft_ms": 100.0},
            "baseline_case_ids": [],
        },
    ]
    facts = {
        "guide-1": {"guide": "pd-disaggregation", "prefill_replicas": 1, "decode_replicas": 1},
        "guide-2": {"guide": "pd-disaggregation", "prefill_replicas": 2, "decode_replicas": 2},
    }
    monkeypatch.setattr(router, "_configuration_facts", lambda case: facts[case["id"]])

    report = router._comparison_report({"cases": cases, "compare_configurations": True})

    assert len(report["topology_comparisons"]) == 1
    row = report["topology_comparisons"][0]
    assert row["left_case_id"] == "guide-1"
    assert row["right_case_id"] == "guide-2"
    assert row["ratio"]["throughput_tps"] == 2.0
    assert row["ratio"]["ttft_ms"] == 0.5


def test_comparison_report_can_disable_pairwise_configuration_comparisons(monkeypatch):
    cases = [
        {"id": "guide-1", "kind": "guide", "workload_ids": ["one"], "metrics": {}, "baseline_case_ids": []},
        {"id": "guide-2", "kind": "guide", "workload_ids": ["two"], "metrics": {}, "baseline_case_ids": []},
    ]
    monkeypatch.setattr(router, "_configuration_facts", lambda _case: {"guide": "pd-disaggregation"})

    report = router._comparison_report({"cases": cases, "compare_configurations": False})

    assert report["topology_comparisons"] == []


def test_evaluate_run_request_defaults_to_no_matrix():
    request = router.EvaluateRunRequest(deployment_execution_id="00000000-0000-4000-8000-000000000001")

    assert request.matrix == []
    assert request.concurrency_stages == []
    assert request.warmup_requests == 2


class _FakeProcess:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_execute_dependencies(monkeypatch, tmp_path, run, subprocess_results):
    """Stub every _execute() collaborator except the matrix/warm-up logic under test."""
    monkeypatch.setattr(
        router, "_get", lambda kind, record_id: run if (kind, record_id) == ("benchmark", run["id"]) else None
    )
    monkeypatch.setattr(router, "_save", lambda _record: None)
    monkeypatch.setattr(router, "_results_root", tmp_path)

    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://endpoint.local"),
        namespace="ns",
        provenance={},
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    monkeypatch.setattr(router, "_execution_model", lambda _execution: "Qwen/Qwen3-0.6B")
    monkeypatch.setattr(router, "_execution_accelerator_profile", lambda _execution, _profile: None)
    monkeypatch.setattr(router, "_harness_proxy_environment", lambda _run, **_kwargs: {})
    monkeypatch.setattr(router, "_harness_proxy_override", lambda _env: None)
    monkeypatch.setattr(router, "_install_helm_proxy_wrapper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(router, "wait_for_deployment_metrics", AsyncMock(return_value=True))
    monkeypatch.setattr(
        router,
        "collect_benchmark_observability",
        AsyncMock(
            return_value={
                "status": "unavailable",
                "reason": "No test telemetry",
                "window": {},
                "summary": {},
                "series": [],
            }
        ),
    )
    monkeypatch.setattr(router.KVTraceCollector, "discover", AsyncMock(side_effect=ValueError("No test probe")))

    async def fake_stream(process, *_args, **_kwargs):
        return await process.communicate()

    monkeypatch.setattr(router, "_stream_benchmark_process", fake_stream)

    async def fake_prepare_runtime(_source):
        return "llmdbenchmark", tmp_path

    async def fake_prepare_storage(*_args, **_kwargs):
        return "standard", None

    async def fake_cleanup_storage(*_args, **_kwargs):
        return None

    monkeypatch.setattr(router, "_prepare_benchmark_runtime", fake_prepare_runtime)
    monkeypatch.setattr(router, "_prepare_benchmark_storage", fake_prepare_storage)
    monkeypatch.setattr(router, "_cleanup_benchmark_storage", fake_cleanup_storage)

    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return subprocess_results[len(calls) - 1]

    monkeypatch.setattr(router.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return calls


@pytest.mark.asyncio
async def test_execute_runs_warmup_before_matrix_points_and_aborts_on_warmup_failure(monkeypatch, tmp_path):
    run = {
        "id": "run-warmup-fail",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 1024, "osl": 128}, {"isl": 8192, "osl": 128}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 2}],
        "warmup_requests": 3,
    }
    calls = _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(1, stderr=b"warm-up boom")])

    await router._execute(run["id"])

    assert len(calls) == 1  # only the warm-up ran; matrix points were never attempted
    assert run["status"] == "failed"
    assert run["warmup"]["status"] == "failed"
    assert run.get("matrix_results", []) == []


@pytest.mark.asyncio
async def test_execute_runs_one_warmup_call_then_one_call_per_matrix_point(monkeypatch, tmp_path):
    run = {
        "id": "run-warmup-ok",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 1024, "osl": 128}, {"isl": 8192, "osl": 128}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 2}],
        "warmup_requests": 3,
    }

    def make_success(workspace_index):
        # Point subprocesses "succeed" with returncode 0; metrics come back empty because no
        # summary_lifecycle_metrics.json was written, which is fine for this test's assertions.
        return _FakeProcess(0)

    calls = _patch_execute_dependencies(
        monkeypatch,
        tmp_path,
        run,
        [_FakeProcess(0), make_success(0), make_success(1)],
    )

    await router._execute(run["id"])

    assert len(calls) == 3  # 1 warm-up + 2 matrix points
    assert run["warmup"]["status"] == "succeeded"
    assert run["warmup"]["isl"] == 1024 and run["warmup"]["osl"] == 128 and run["warmup"]["num_requests"] == 3
    assert run["status"] == "succeeded"
    assert [point["isl"] for point in run["matrix_results"]] == [1024, 8192]
    assert all(point["status"] == "succeeded" for point in run["matrix_results"])


@pytest.mark.asyncio
async def test_execute_skips_warmup_when_warmup_requests_is_zero(monkeypatch, tmp_path):
    run = {
        "id": "run-no-warmup",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 1024, "osl": 128}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 2}],
        "warmup_requests": 0,
    }
    calls = _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(0)])

    await router._execute(run["id"])

    assert len(calls) == 1  # only the single matrix point; no warm-up call
    assert "warmup" not in run
    assert run["status"] == "succeeded"


@pytest.mark.asyncio
async def test_execute_routes_model_service_runs_through_the_gateway_regardless_of_data_plane(monkeypatch, tmp_path):
    # An "existing endpoint" run targets a published Model Service: its
    # HTTPRoute reaches the InferencePool directly, so the harness must use
    # the shared Gateway even though the underlying deployment's recorded (or
    # declared) data plane says it normally keeps its own proxy.
    run = {
        "id": "run-model-service",
        "deployment_execution_id": "exec-1",
        "model_service_group_id": "msg-1",
        "model_service_published_name": "Qwen/Qwen3-0.6B",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 1024, "osl": 128}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 2}],
        "warmup_requests": 0,
    }
    _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(0)])
    monkeypatch.setattr(router, "deployment_uses_shared_gateway", lambda _execution: False)
    monkeypatch.setattr(router, "_cluster_gateway_endpoint", AsyncMock(return_value="http://gateway.local"))

    await router._execute(run["id"])

    assert run["endpoint_used"] == "http://gateway.local"
    assert run["status"] == "succeeded"


@pytest.mark.asyncio
async def test_execute_keeps_deployment_endpoint_for_design_configuration_runs(monkeypatch, tmp_path):
    # A Design Configuration run (no model_service_group_id) falls back to the
    # deployment's own data plane: it must not be routed through the Gateway
    # just because one happens to be ready in the cluster.
    run = {
        "id": "run-design-config",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 1024, "osl": 128}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 2}],
        "warmup_requests": 0,
    }
    _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(0)])
    monkeypatch.setattr(router, "deployment_uses_shared_gateway", lambda _execution: False)
    monkeypatch.setattr(router, "_cluster_gateway_endpoint", AsyncMock(return_value="http://gateway.local"))

    await router._execute(run["id"])

    assert run["endpoint_used"] == "http://endpoint.local"
    assert run["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("matrix", [True, False])
async def test_execute_isolates_cluster_process_from_harness_proxies(monkeypatch, tmp_path, matrix):
    import os

    proxy_environment = router._harness_proxy_environment
    proxy_override = router._harness_proxy_override
    install_wrapper = router._install_helm_proxy_wrapper
    run = {
        "id": "proxy-isolation",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "matrix": [{"isl": 1024, "osl": 128}],
        "concurrency_stages": [{"concurrency": 1, "num_requests": 2}],
        "warmup_requests": 1,
        "https_proxy": "http://harness-proxy.invalid:911",
    }
    if not matrix:
        run.pop("matrix")
    _patch_execute_dependencies(monkeypatch, tmp_path, run, [])
    monkeypatch.setattr(router, "_harness_proxy_environment", proxy_environment)
    monkeypatch.setattr(router, "_harness_proxy_override", proxy_override)
    monkeypatch.setattr(router, "_install_helm_proxy_wrapper", install_wrapper)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://ambient-proxy.invalid:911")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("KUBECONFIG", "/selected/cluster.yaml")
    captured = []

    async def capture_process(*args, **kwargs):
        captured.append((args, kwargs["env"]))
        return _FakeProcess(1, stderr=b"stop after capturing launch")

    monkeypatch.setattr(router.asyncio, "create_subprocess_exec", capture_process)
    await router._execute(run["id"])

    assert len(captured) == 1
    command, environment = captured[0]
    assert not any(name.lower() in {"http_proxy", "https_proxy", "all_proxy"} for name in environment)
    assert environment["KUBECONFIG"] == "/selected/cluster.yaml"
    assert "--envvarspod" not in command
    override = next(arg for arg in command if arg.startswith("harness.extraEnvVars="))
    pod_env = {entry["name"]: entry["value"] for entry in json.loads(override.split("=", 1)[1])}
    assert pod_env["HTTPS_PROXY"] == "http://harness-proxy.invalid:911"
    assert ".svc" in pod_env["NO_PROXY"].split(",")
    assert os.environ["HTTPS_PROXY"] == "http://ambient-proxy.invalid:911"


def test_deployment_uses_shared_gateway_from_contract_and_ownership():
    from llm_d_bench.deploy.data_plane import deployment_uses_shared_gateway

    def execution(provenance, shares_gateway):
        return SimpleNamespace(
            provenance=provenance,
            artifact=SimpleNamespace(
                rendered_payload=SimpleNamespace(value={"deployment_contract": {"shares_gateway": shares_gateway}})
            ),
        )

    # A provider that declares the shared Gateway routes through it...
    assert deployment_uses_shared_gateway(execution({}, True)) is True
    # ...unless the deployment is evaluation-owned (keeps its own proxy).
    assert deployment_uses_shared_gateway(execution({"evaluate_workflow": True}, True)) is False
    # Providers with their own proxy never use the Gateway.
    assert deployment_uses_shared_gateway(execution({}, False)) is False
    assert deployment_uses_shared_gateway(SimpleNamespace(provenance={}, artifact=None)) is False
