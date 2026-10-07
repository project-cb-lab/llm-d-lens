"""Entry-point parity: ownership controls cleanup, not measurement or analysis."""

import asyncio
import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from llm_d_bench.deploy.contracts import DeployableConfiguration, DeploymentStatus
from llm_d_bench.evaluate.test_matrix import _FakeProcess, _patch_execute_dependencies
from llm_d_bench.utils.artifacts import configuration_checksum

router = importlib.import_module("llm_d_bench.evaluate.router")
profiling = importlib.import_module("llm_d_bench.monitoring.profiling.service")
OWNERS = ["existing-endpoint", "evaluation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", OWNERS)
@pytest.mark.parametrize("already_ready", [True, False])
async def test_monitoring_setup_is_shared_and_reuses_healthy_targets(monkeypatch, ownership, already_ready):
    run = {"deployment_execution_id": "execution", "deployment_ownership": ownership}
    monkeypatch.setattr(router, "_save", lambda _: None)
    ready = AsyncMock(side_effect=[already_ready, True] if not already_ready else [True])
    enable = AsyncMock(return_value={"podmonitors": ["model"]})
    monkeypatch.setattr(router, "wait_for_deployment_metrics", ready)
    monkeypatch.setattr(router.deployment_monitoring, "enable", enable)
    await router._prepare_benchmark_monitoring(run)
    assert run["monitoring"]["status"] == "ready"
    assert run["monitoring"]["enabled"] is True
    if already_ready:
        enable.assert_not_awaited()
    else:
        enable.assert_awaited_once_with("execution")
        assert run["monitoring"]["resources"]["podmonitors"] == ["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", OWNERS)
async def test_monitoring_discovery_is_bounded_and_does_not_fail_run(monkeypatch, ownership):
    cancelled = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(router, "_save", lambda _: None)
    monkeypatch.setattr(router, "wait_for_deployment_metrics", blocked)
    monkeypatch.setattr(router, "_MONITORING_PREPARE_TIMEOUT", 0.01)
    run = {"deployment_execution_id": "execution", "deployment_ownership": ownership}
    await router._prepare_benchmark_monitoring(run)
    assert cancelled.is_set()
    assert run["monitoring"]["status"] == "unavailable"
    assert "exceeded" in run["monitoring"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", OWNERS)
async def test_monitoring_cancellation_is_not_swallowed(monkeypatch, ownership):
    monkeypatch.setattr(router, "_save", lambda _: None)
    monkeypatch.setattr(router, "wait_for_deployment_metrics", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await router._prepare_benchmark_monitoring(
            {"deployment_execution_id": "execution", "deployment_ownership": ownership}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", OWNERS)
async def test_runner_collects_existing_metrics_even_when_setup_fails(monkeypatch, tmp_path, ownership):
    run = {
        "id": "parity",
        "deployment_execution_id": "execution",
        "deployment_ownership": ownership,
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
    }
    calls = _patch_execute_dependencies(monkeypatch, tmp_path, run, [_FakeProcess(0)])
    run["cluster_session_id"] = "session"
    monkeypatch.setattr(router, "wait_for_deployment_metrics", AsyncMock(return_value=False))
    monkeypatch.setattr(router.deployment_monitoring, "enable", AsyncMock(side_effect=ValueError("RBAC forbidden")))
    observation = {
        "status": "available",
        "window": {"start": "2026-09-16T00:00:00Z", "end": "2026-09-16T00:01:00Z"},
        "series": [],
        "summary": {"queue_depth": {"mean": 2}},
    }
    collect = AsyncMock(return_value=observation)
    monkeypatch.setattr(router, "collect_benchmark_observability", collect)
    snapshot = {"source": "kubernetes-api", "pods": [{"name": "model"}]}
    monkeypatch.setattr(router, "_kubernetes_resource_snapshot", AsyncMock(return_value=snapshot))
    await router._execute(run["id"])
    assert run["status"] == "succeeded", run.get("error")
    assert len(calls) == 1
    collect.assert_awaited_once()
    assert collect.call_args.args[0] == "execution"
    assert run["metrics"]["observability"] == observation
    assert run["metrics"]["system_metrics"]["queue_depth"] == 2
    assert run["resource_snapshot"] == snapshot
    assert run["deployment_ownership"] == ownership
    assert (tmp_path / run["id"] / "summary_kv_access.json").exists()


def make_record(ownership):
    content = {
        "model": {"name": "test-model"},
        "prefill": {"replicaCount": 1, "tensorParallelSize": 2},
        "decode": {"replicaCount": 2, "tensorParallelSize": 1},
    }
    configuration = DeployableConfiguration(
        type="guide",
        provider_ref="pd-disaggregation",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )
    return {
        "id": "run",
        "kind": "benchmark",
        "status": "succeeded",
        "deployment_ownership": ownership,
        "deployment_configuration": configuration.model_dump(mode="json"),
        "metrics": {},
        "sla_targets": {"success_rate_min_percent": 99},
        "rate_stage_results": [
            {"rate": 10, "metrics": {"throughput_rps": 9.8, "success_rate": 100}},
            {"rate": 20, "metrics": {"throughput_rps": 15, "success_rate": 96}},
        ],
    }


@pytest.mark.asyncio
async def test_standalone_and_workflow_detail_have_identical_enrichment(monkeypatch):
    standalone = make_record("existing-endpoint")
    owned = make_record("evaluation")
    case = {**deepcopy(owned), "kind": "guide", "evaluation_run_id": "run", "workload_ids": ["workload"]}
    workflow = {"id": "workflow", "status": "succeeded", "cases": [case]}
    monkeypatch.setattr(router, "_save", lambda _: None)
    monkeypatch.setattr(router, "_get", lambda kind, _: workflow if kind == "workflow" else standalone)
    single = await router.get_run("run")
    monkeypatch.setattr(router, "_get", lambda kind, _: workflow if kind == "workflow" else owned)
    details = await router.get_workflow_run_details("workflow")
    assert single["metrics"] == details["cases"][0]["case"]["metrics"] == details["cases"][0]["evaluation"]["metrics"]
    assert single["metrics"]["maximum_stable_qps"] == 10
    assert single["metrics"]["slo_goodput_rps"] == 9.8
    assert single["metrics"]["mechanism_metrics"]["pd_topology"] == "1P×TP2 / 2D×TP1"
    assert single["metrics"]["diagnoses"]


@pytest.mark.asyncio
async def test_standalone_hydrates_read_only_context_without_recapturing_history(monkeypatch):
    run = {
        "id": "run",
        "kind": "benchmark",
        "status": "succeeded",
        "deployment_execution_id": "execution",
        "deployment_ownership": "existing-endpoint",
        "metrics": {},
    }
    configuration = DeployableConfiguration.model_validate(make_record("evaluation")["deployment_configuration"])
    execution = SimpleNamespace(
        namespace="model-ns",
        status=DeploymentStatus.READY,
        monitoring_setup={"status": "enabled"},
        diagnostics=None,
        configuration_artifacts=[],
        endpoint=SimpleNamespace(url="http://model", model_ref="test-model"),
    )
    deployment = SimpleNamespace(
        id="deployment",
        source_configurations=[configuration],
        cases=[SimpleNamespace(id="case", execution_id="execution", source_configuration_ordinal=0)],
    )
    monkeypatch.setattr(
        router, "_store", SimpleNamespace(get_execution=lambda _: execution, list_runs=lambda: [deployment])
    )
    monkeypatch.setattr(router, "_get", lambda *_: run)
    monkeypatch.setattr(router, "_save", lambda _: None)
    capture = AsyncMock(side_effect=AssertionError("GET must not recapture historical resources"))
    monkeypatch.setattr(router, "_kubernetes_resource_snapshot", capture)
    result = await router.get_run("run")
    assert result["namespace"] == "model-ns"
    assert result["configuration"]["guide"] == "pd-disaggregation"
    assert result["configuration"]["tensor_parallel_size"] == 1
    assert result["deployment_cases"][0]["execution_status"] == "ready"
    assert result["deployment_ownership"] == "existing-endpoint"
    assert not result.get("resource_snapshot")
    capture.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [False, True])
async def test_prometheus_readiness_requires_up_one(monkeypatch, healthy):
    monkeypatch.setattr(profiling, "_deployment_target", lambda *_: (None, "model-ns", "cluster"))
    monkeypatch.setattr(profiling, "_prometheus_local_port", AsyncMock(return_value=12345))

    async def query(_client, expression):
        assert expression == 'sum(up{namespace="model-ns"} == 1)'
        return [{"value": [0, "1"]}] if healthy else []

    monkeypatch.setattr(profiling, "_query", query)
    assert await profiling.wait_for_deployment_metrics("execution", timeout_seconds=0) is healthy


def test_openapi_describes_shared_result_contract():
    app = FastAPI()
    app.include_router(router.router)
    paths = app.openapi()["paths"]
    assert "same runner" in paths["/api/v1/evaluate/runs"]["post"]["description"]
    assert "deployment_cases" in paths["/api/v1/evaluate/runs/{run_id}"]["get"]["description"]


def test_workflow_and_standalone_share_all_workload_defaults_and_validation():
    from llm_d_bench.evaluate.models import BenchmarkSpec

    request = router.EvaluateRunRequest(deployment_execution_id="execution")
    assert request.model_dump(include=set(BenchmarkSpec.model_fields)) == BenchmarkSpec().model_dump()
    for field in BenchmarkSpec.model_fields:
        assert (
            router.EvaluateRunRequest.model_json_schema()["properties"][field]
            == BenchmarkSpec.model_json_schema()["properties"][field]
        )
