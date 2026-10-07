"""Tests for the open-loop, shared-system-prompt rate-ramp workload and its comparison
statistics. Mirrors test_matrix.py's structure for the closed-loop ISL x OSL x concurrency
sweep -- this is the guide-agnostic counterpart used to stress prefix-cache reuse/offloading
instead of exact-length random requests.
"""

import importlib
import json
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from llm_d_bench.evaluate.models import BenchmarkSpec, RateStage, SharedPrefixWorkloadSpec, WorkloadMatrixPoint

router = importlib.import_module("llm_d_bench.evaluate.router")


def test_rate_stage_and_shared_prefix_spec_validate_bounds():
    with pytest.raises(ValidationError):
        RateStage(rate=0, duration=60)
    with pytest.raises(ValidationError):
        SharedPrefixWorkloadSpec(
            num_groups=0,
            num_prompts_per_group=5,
            system_prompt_len=3000,
            question_len=256,
            output_len=256,
            stages=[RateStage(rate=1.0, duration=60)],
        )
    with pytest.raises(ValidationError):
        SharedPrefixWorkloadSpec(
            num_groups=60,
            num_prompts_per_group=5,
            system_prompt_len=3000,
            question_len=256,
            output_len=256,
            stages=[],
        )
    stage = RateStage(rate=2.0, duration=60)
    assert stage.rate == 2.0 and stage.duration == 60


def test_benchmark_spec_rejects_matrix_and_shared_prefix_together():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        BenchmarkSpec(
            matrix=[WorkloadMatrixPoint(isl=1024, osl=128)],
            shared_prefix=SharedPrefixWorkloadSpec(
                num_groups=60,
                num_prompts_per_group=5,
                system_prompt_len=3000,
                question_len=256,
                output_len=256,
                stages=[RateStage(rate=1.0, duration=60)],
            ),
        )


@pytest.mark.parametrize("legacy_settings", [{}, {"interval": 60}])
def test_shared_prefix_workload_yaml_is_open_loop_with_reused_system_prompts(legacy_settings):
    spec = SharedPrefixWorkloadSpec(
        num_groups=60,
        num_prompts_per_group=5,
        system_prompt_len=3000,
        question_len=256,
        output_len=256,
        enable_multi_turn_chat=False,
        **legacy_settings,
        stages=[RateStage(rate=1.0, duration=60), RateStage(rate=2.0, duration=60)],
    )

    content = router._shared_prefix_workload_yaml(spec, "Qwen/Qwen3-32B", "http://endpoint.local")
    parsed = yaml.safe_load(content)

    assert parsed["load"] == {
        "type": "poisson",
        "interval": 0,
        "num_workers": 1,
        "worker_max_concurrency": 8,
        "worker_max_tcp_connections": 32,
        "request_timeout": 120,
        "stages": [{"rate": 1.0, "duration": 60}, {"rate": 2.0, "duration": 60}],
    }
    assert parsed["server"]["model_name"] == "Qwen/Qwen3-32B"
    assert parsed["server"]["base_url"] == "http://endpoint.local"
    assert parsed["data"]["type"] == "shared_prefix"
    assert parsed["data"]["shared_prefix"] == {
        "num_groups": 60,
        "num_prompts_per_group": 5,
        "system_prompt_len": 3000,
        "question_len": 256,
        "output_len": 256,
        "enable_multi_turn_chat": False,
    }
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
                "time_to_first_token": {
                    "mean": ttft_mean_s,
                    "median": ttft_mean_s,
                    "p90": (ttft_mean_s + ttft_p99_s) / 2,
                    "p99": ttft_p99_s,
                },
                "time_per_output_token": {"mean": 0.05, "median": 0.05, "p90": 0.055, "p99": 0.06},
                "request_latency": {"mean": 1.5, "median": 1.5, "p90": 1.65, "p99": 1.8},
                "inter_token_latency": {"mean": 0.05, "median": 0.05, "p99": 0.06},
                "normalized_time_per_output_token": {"mean": 0.05, "median": 0.05, "p99": 0.06},
            },
        },
        "failures": {"count": 0},
    }
    (directory / f"stage_{stage_index}_lifecycle_metrics.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / "summary_lifecycle_metrics.json").write_text(json.dumps(payload), encoding="utf-8")


def _rate_stage(
    tmp_path, stage_index, rate, duration, *, throughput, ttft_mean_s, ttft_p99_s, total_output_tokens, duration_s
):
    _write_stage_metrics(
        tmp_path,
        stage_index,
        output_tokens_per_sec=throughput,
        ttft_mean_s=ttft_mean_s,
        ttft_p99_s=ttft_p99_s,
        total_output_tokens=total_output_tokens,
        duration_s=duration_s,
    )
    return {"rate": rate, "duration": duration, "metrics": router._stage_metric_summary(tmp_path, stage_index)}


def test_rate_stage_comparison_computes_geometric_mean_and_suite_normalized_throughput(tmp_path):
    guide_stages = [
        _rate_stage(
            tmp_path,
            0,
            1.0,
            60,
            throughput=200.0,
            ttft_mean_s=0.5,
            ttft_p99_s=0.6,
            total_output_tokens=2000,
            duration_s=10.0,
        ),
        _rate_stage(
            tmp_path,
            1,
            2.0,
            60,
            throughput=100.0,
            ttft_mean_s=1.0,
            ttft_p99_s=1.2,
            total_output_tokens=1000,
            duration_s=10.0,
        ),
    ]
    baseline_stages = [
        _rate_stage(
            tmp_path,
            0,
            1.0,
            60,
            throughput=100.0,
            ttft_mean_s=1.0,
            ttft_p99_s=1.2,
            total_output_tokens=1000,
            duration_s=10.0,
        ),
        _rate_stage(
            tmp_path,
            1,
            2.0,
            60,
            throughput=50.0,
            ttft_mean_s=2.0,
            ttft_p99_s=2.4,
            total_output_tokens=500,
            duration_s=10.0,
        ),
    ]

    statistics = router._rate_stage_comparison(guide_stages, baseline_stages)

    assert statistics["stage_count"] == 2
    assert statistics["geometric_mean_ratio"]["throughput_tps"] == pytest.approx(2.0)
    assert statistics["geometric_mean_ratio"]["ttft_ms"] == pytest.approx(0.5)
    assert statistics["geometric_mean_ratio_p90"]["ttft_ms"] == pytest.approx(0.5)
    assert statistics["geometric_mean_ratio_p99"]["ttft_ms"] == pytest.approx(0.5)
    assert statistics["suite_normalized_throughput_ratio"] == pytest.approx(2.0)
    assert statistics["rows"][0]["rate"] == 1.0
    assert statistics["rows"][0]["guide"]["throughput_tps"] == 200.0
    assert statistics["rows"][0]["baseline"]["throughput_tps"] == 100.0
    assert statistics["rows"][0]["guide"]["ttft_ms"] == 500.0
    assert statistics["rows"][0]["baseline"]["ttft_ms"] == 1000.0
    assert statistics["rows"][0]["guide_p90"]["ttft_ms"] == 550.0
    assert statistics["rows"][0]["baseline_p90"]["ttft_ms"] == 1100.0
    assert statistics["rows"][0]["guide_p90"]["request_latency_ms"] == 1650.0
    assert statistics["rows"][0]["baseline_p90"]["request_latency_ms"] == 1650.0
    assert statistics["rows"][1]["rate"] == 2.0


def test_rate_stage_comparison_returns_none_without_both_sides():
    assert router._rate_stage_comparison([], [{"rate": 1.0, "metrics": {}}]) is None
    assert router._rate_stage_comparison([{"rate": 1.0, "metrics": {}}], []) is None


def test_capacity_metrics_find_stable_goodput_violation_and_saturation():
    record = {
        "sla_targets": {"ttft_ms": 500, "ttft_percentile": "p99", "success_rate_min_percent": 99},
        "metrics": {},
        "rate_stage_results": [
            {
                "rate": 10,
                "metrics": {
                    "throughput_rps": 9.8,
                    "success_rate": 100,
                    "latency_distributions": {"ttft": {"p99_ms": 400}},
                },
            },
            {
                "rate": 20,
                "metrics": {
                    "throughput_rps": 19.4,
                    "success_rate": 99.5,
                    "latency_distributions": {"ttft": {"p99_ms": 450}},
                },
            },
            {
                "rate": 30,
                "metrics": {
                    "throughput_rps": 24,
                    "success_rate": 96,
                    "latency_distributions": {"ttft": {"p99_ms": 900}},
                },
            },
        ],
    }

    assert router._apply_capacity_metrics(record) is True
    assert record["metrics"]["maximum_stable_qps"] == 20
    assert record["metrics"]["slo_goodput_rps"] == 19.4
    assert record["metrics"]["slo_violation_point"] == 30
    assert record["metrics"]["saturation_point"] == 30


def test_rate_stage_observability_is_aligned_and_added_to_stage_metrics():
    record = {
        "rate_stage_results": [
            {"rate": 10, "duration": 10, "metrics": {}},
            {"rate": 20, "duration": 10, "metrics": {}},
        ]
    }
    observability = {
        "window": {"start": "2026-09-02T00:00:00Z", "end": "2026-09-02T00:00:20Z"},
        "series": [
            {"timestamp": "2026-09-02T00:00:05Z", "queue_depth": 0, "running_requests": 2},
            {"timestamp": "2026-09-02T00:00:15Z", "queue_depth": 4, "running_requests": 6},
        ],
    }
    router._attach_rate_stage_observability(record, observability)
    assert record["rate_stage_results"][0]["metrics"]["queue_depth"] == 0
    assert record["rate_stage_results"][1]["metrics"]["queue_depth"] == 4
    assert record["rate_stage_results"][1]["observability"]["summary"]["running_requests"]["p99"] == 6


def test_legacy_rate_stage_report_requires_refresh():
    legacy = {
        "comparisons": [
            {
                "rate_stage_statistics": {"rows": [{"rate": 1.0, "ratio": {"ttft_ms": 0.5}}]},
            }
        ],
    }
    current = {
        "comparison_version": 2,
        "comparisons": [
            {
                "guide_configuration": {"guide": "tiered-prefix-cache"},
                "baseline_configuration": {"guide_variant": "base"},
                "parity": {"valid": True, "checks": {}},
                "mechanism_delta_percent": {},
                "rate_stage_statistics": {"rows": [{"rate": 1.0, "guide": {}, "baseline": {}}]},
            }
        ],
    }

    assert router._comparison_report_needs_refresh(legacy) is True
    assert router._comparison_report_needs_refresh(current) is False


def test_comparison_parity_requires_same_workload_runtime_and_accelerator_budget():
    common = {
        "model": "Qwen/Qwen3-32B",
        "runtime": "vllm",
        "accelerator": "xpu",
        "replicas": 2,
        "tensor_parallel_size": 4,
        "benchmark": {"workload": "shared-prefix"},
    }
    assert router._comparison_parity(common, dict(common))["valid"] is True

    unequal = {**common, "replicas": 1}
    result = router._comparison_parity(common, unequal)
    assert result["valid"] is False
    assert result["checks"]["accelerator_count"] is False


def test_tiered_mechanism_uses_runtime_cache_capacity_for_working_set_ratios():
    record = {
        "configuration": {"guide": "tiered-prefix-cache", "guideVariant": "native-cpu"},
        "benchmark": {"shared_prefix": {"num_groups": 4, "system_prompt_len": 1000}},
        "metrics": {
            "observability": {
                "cache_config": {
                    "hbm_capacity_tokens": 2000,
                    "effective_capacity_tokens": 5000,
                }
            }
        },
    }

    assert router._apply_guide_mechanism_metrics(record) is True
    mechanism = record["metrics"]["mechanism_metrics"]
    assert mechanism["working_set_hbm_ratio"] == 2.0
    assert mechanism["working_set_effective_capacity_ratio"] == 0.8
    assert mechanism["hbm_capacity"]["status"] == "runtime reported"
    assert record["metrics"]["evidence_contract"]["hbm_capacity"]["value"]["value"] == 2000


def test_evaluate_run_request_defaults_to_no_shared_prefix():
    request = router.EvaluateRunRequest(deployment_execution_id="00000000-0000-4000-8000-000000000001")

    assert request.shared_prefix is None


class _FakeProcess:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_execute_dependencies(monkeypatch, tmp_path, run, subprocess_results):
    """Stub every _execute() collaborator except the shared_prefix logic under test."""
    from unittest.mock import AsyncMock

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
@pytest.mark.parametrize("memory", [None, 64])
async def test_execute_runs_a_single_invocation_covering_every_rate_stage(monkeypatch, tmp_path, memory):
    run = {
        "id": "run-shared-prefix",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "shared_prefix": {
            "num_groups": 60,
            "num_prompts_per_group": 5,
            "system_prompt_len": 3000,
            "question_len": 256,
            "output_len": 256,
            "stages": [{"rate": 1.0, "duration": 60}, {"rate": 2.0, "duration": 60}],
        },
    }
    if memory is not None:
        run["harness_memory_gib"] = memory
    calls = _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(0)])

    await router._execute(run["id"])

    assert f"harness.resources.memory={memory or 32}Gi" in calls[0]
    assert len(calls) == 1  # every rate stage rides the same llmdbenchmark invocation
    assert run["status"] == "succeeded"
    assert (tmp_path / run["id"] / "workload.yaml").is_file()
    assert len(run["rate_stage_results"]) == 2
    assert [stage["rate"] for stage in run["rate_stage_results"]] == [1.0, 2.0]


@pytest.mark.asyncio
async def test_execute_fails_the_run_when_shared_prefix_invocation_fails(monkeypatch, tmp_path):
    run = {
        "id": "run-shared-prefix-fail",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "shared_prefix": {
            "num_groups": 60,
            "num_prompts_per_group": 5,
            "system_prompt_len": 3000,
            "question_len": 256,
            "output_len": 256,
            "stages": [{"rate": 1.0, "duration": 60}],
        },
    }
    calls = _patch_execute_dependencies(
        monkeypatch,
        tmp_path,
        run,
        [
            _FakeProcess(
                1,
                stderr=b"Found pods in error state: inference-perf-test/harness (terminated: OOMKilled, exit_code=137)",
            )
        ],
    )

    await router._execute(run["id"])

    assert len(calls) == 1
    assert run["status"] == "failed"
    assert "load generator (harness) was OOMKilled" in run["error"]
    assert "32 GiB" in run["error"]
    assert "exit_code=137" in run["stderr"]
    assert "rate_stage_results" not in run


def test_configured_slo_without_measurement_cannot_prove_stability():
    import importlib

    router = importlib.import_module("llm_d_bench.evaluate.router")
    metrics = {"success_rate": 100, "throughput_rps": 10}
    targets = {"ttft_ms": 100, "success_rate_min_percent": 99}
    assert router._sla_evaluation(metrics, targets)["met"] is None
    record = {"metrics": {}, "sla_targets": targets, "rate_stage_results": [{"rate": 10, "metrics": metrics}]}
    router._apply_capacity_metrics(record)
    assert record["metrics"]["capacity_analysis"]["maximum_stable_qps"] is None


def test_default_serialized_slo_keeps_success_threshold():
    import importlib

    from llm_d_bench.evaluate.models import BenchmarkSlaTargets

    router = importlib.import_module("llm_d_bench.evaluate.router")
    record = {
        "metrics": {},
        "sla_targets": BenchmarkSlaTargets().model_dump(),
        "rate_stage_results": [{"rate": 10, "metrics": {"success_rate": 100, "throughput_rps": 10}}],
    }
    router._apply_capacity_metrics(record)
    assert record["metrics"]["capacity_analysis"]["maximum_stable_qps"] == 10


@pytest.mark.asyncio
async def test_completed_history_is_saved_before_storage_cleanup_failure(monkeypatch, tmp_path):
    from copy import deepcopy

    run = {
        "id": "retained-history",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "shared_prefix": {
            "num_groups": 2,
            "num_prompts_per_group": 2,
            "system_prompt_len": 128,
            "question_len": 32,
            "output_len": 32,
            "stages": [{"rate": 1.0, "duration": 60}],
        },
    }
    _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(0)])
    saved = []
    monkeypatch.setattr(router, "_save", lambda record: saved.append(deepcopy(record)))

    async def cleanup(*_args):
        assert saved[-1]["status"] == "succeeded"
        assert saved[-1]["rate_stage_results"] == run["rate_stage_results"]
        assert saved[-1]["metrics"] == run["metrics"]
        raise RuntimeError("PVC cleanup unavailable")

    monkeypatch.setattr(router, "_cleanup_benchmark_storage", cleanup)
    with pytest.raises(RuntimeError, match="PVC cleanup unavailable"):
        await router._execute(run["id"])
    assert saved[-1]["status"] == "succeeded"
    assert (tmp_path / run["id"] / "workload.yaml").is_file()
