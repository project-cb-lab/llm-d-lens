"""Tests for the `kubernetes-service` baseline: reuses a sibling Guide case's already-ready
deployment execution (no second deployment) and hits the Guide's registered baseline
endpoint -- the only way to match a Guide's own "vs a stock Kubernetes Service" benchmark
methodology (currently precise-prefix-cache-routing) without provisioning a second stack.
"""

import importlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from llm_d_bench.deploy.contracts import DeployableConfiguration
from llm_d_bench.evaluate.models import BenchmarkPlan, EvaluationCreateRequest
from llm_d_bench.utils.artifacts import configuration_checksum

router = importlib.import_module("llm_d_bench.evaluate.router")


def test_kubernetes_service_is_an_accepted_baseline_type():
    content = {
        "model": {"name": "Qwen/Qwen3-30B-A3B"},
        "decode": {"replicaCount": 8, "tensorParallelSize": 4},
        "runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.9.0"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="precise-prefix-cache-routing",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )
    artifact_id = "00000000-0000-4000-8000-000000000001"
    request = EvaluationCreateRequest(
        cluster_session_id="00000000-0000-4000-8000-000000000002",
        benchmark_plans=[
            {
                "id": "candidate",
                "configuration_artifact_id": artifact_id,
                "baseline_types": ["kubernetes-service"],
            }
        ],
    )

    cases = router._evaluation_cases(request, {artifact_id: SimpleNamespace(deployable_configuration=configuration)})

    assert cases[0]["kind"] == "guide"
    assert cases[0]["baseline_case_ids"] == ["baseline-1"]
    assert cases[1]["kind"] == "baseline"
    assert cases[1]["baseline_type"] == "kubernetes-service"
    assert cases[1]["dependent_guide_case_id"] == "guide-1"


def test_kubernetes_service_baseline_is_not_deduplicated_across_candidates():
    content = {
        "model": {"name": "Qwen/Qwen3-30B-A3B"},
        "decode": {"replicaCount": 8, "tensorParallelSize": 4},
        "runtime": {"image": "image:test"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="precise-prefix-cache-routing",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )
    artifact_ids = [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]
    request = EvaluationCreateRequest(
        cluster_session_id="00000000-0000-4000-8000-000000000003",
        benchmark_plans=[
            {
                "id": f"candidate-{index}",
                "configuration_artifact_id": artifact_id,
                "baseline_types": ["kubernetes-service"],
            }
            for index, artifact_id in enumerate(artifact_ids, start=1)
        ],
    )

    cases = router._evaluation_cases(
        request,
        {artifact_id: SimpleNamespace(deployable_configuration=configuration) for artifact_id in artifact_ids},
    )
    ordered = router._ordered_evaluation_cases(cases)

    assert [case["id"] for case in ordered] == ["guide-1", "baseline-1", "guide-2", "baseline-2"]
    assert ordered[1]["dependent_guide_case_id"] == "guide-1"
    assert ordered[3]["dependent_guide_case_id"] == "guide-2"


def test_baseline_type_rejects_unknown_values():
    with pytest.raises(ValidationError):
        BenchmarkPlan(
            configuration_artifact_id="00000000-0000-4000-8000-000000000001",
            baseline_types=["not-a-real-baseline-type"],
        )


def _workflow(guide_case: dict, baseline_case: dict) -> dict:
    guide_case.setdefault("baseline_case_ids", [baseline_case["id"]])
    return {
        "id": "workflow-1",
        "cluster_session_id": "session-1",
        "runtime": {"http_proxy": "", "https_proxy": "", "no_proxy": ""},
        "cases": [guide_case, baseline_case],
    }


def _baseline_case(**overrides) -> dict:
    case = {
        "id": "baseline-1",
        "kind": "baseline",
        "status": "queued",
        "baseline_type": "kubernetes-service",
        "benchmark": {
            "harness": "inference-perf",
            "workload": "guide_precise-prefix-cache-routing_1.yaml",
            "parallelism": 1,
            "wait_timeout_seconds": 60,
            "warmup_requests": 0,
        },
    }
    case.update(overrides)
    return case


@pytest.mark.asyncio
async def test_shared_pods_baseline_fails_clearly_without_a_ready_sibling_guide_case(monkeypatch):
    saved = []
    monkeypatch.setattr(router, "_save", saved.append)
    guide_case = {"id": "guide-1", "kind": "guide", "status": "queued"}
    baseline_case = _baseline_case()
    workflow = _workflow(guide_case, baseline_case)

    await router._execute_shared_pods_baseline_case(workflow, baseline_case)

    assert baseline_case["status"] == "failed"
    assert "no ready Guide deployment" in baseline_case["error"]


@pytest.mark.asyncio
async def test_shared_pods_baseline_fails_clearly_when_guide_has_no_baseline_endpoint(monkeypatch):
    saved = []
    monkeypatch.setattr(router, "_save", saved.append)
    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://guide.local", baseline_url=None),
        namespace="ns",
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    guide_case = {"id": "guide-1", "kind": "guide", "status": "succeeded", "execution_id": "exec-1"}
    baseline_case = _baseline_case()
    workflow = _workflow(guide_case, baseline_case)

    await router._execute_shared_pods_baseline_case(workflow, baseline_case)

    assert baseline_case["status"] == "failed"
    assert "kubernetes-service baseline endpoint" in baseline_case["error"]


@pytest.mark.asyncio
async def test_shared_pods_baseline_reuses_guide_execution_and_hits_baseline_url(monkeypatch):
    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://guide.local", baseline_url="http://guide-baseline.local"),
        namespace="llmd-precise-prefix-cache-routing-run",
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    monkeypatch.setattr(router, "_save", lambda _record: None)
    monkeypatch.setattr(router, "_kubernetes_resource_snapshot", lambda *_args, **_kwargs: _async_none())

    captured_requests = []

    async def fake_create_run(request):
        captured_requests.append(request)
        return {"id": "benchmark-1"}

    async def fake_wait_for_benchmark(_benchmark_id):
        return {"status": "succeeded", "metrics": {"throughput_tps": 42}, "rate_stage_results": [{"rate": 1.0}]}

    monkeypatch.setattr(router, "create_run", fake_create_run)
    monkeypatch.setattr(router, "_wait_for_benchmark", fake_wait_for_benchmark)

    guide_case = {"id": "guide-1", "kind": "guide", "status": "succeeded", "execution_id": "exec-1"}
    baseline_case = _baseline_case()
    baseline_case["benchmark"]["harness_memory_gib"] = 64
    workflow = _workflow(guide_case, baseline_case)

    await router._execute_shared_pods_baseline_case(workflow, baseline_case)

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.deployment_execution_id == "exec-1"
    assert request.harness_memory_gib == 64
    assert request.use_baseline_endpoint is True
    assert baseline_case["status"] == "succeeded"
    assert baseline_case["execution_id"] == "exec-1"
    assert baseline_case["endpoint"] == "http://guide-baseline.local"
    assert baseline_case["endpoint_kind"] == "kubernetes-service"
    assert baseline_case["metrics"] == {"throughput_tps": 42}
    assert baseline_case["rate_stage_results"] == [{"rate": 1.0}]
    # Never owns a deployment -- nothing here should ever be cleaned up.
    assert "deployment_run_id" not in baseline_case or baseline_case.get("deployment_run_id") is None


@pytest.mark.asyncio
async def test_shared_pods_baseline_raises_when_the_benchmark_fails(monkeypatch):
    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://guide.local", baseline_url="http://guide-baseline.local"),
        namespace="ns",
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    monkeypatch.setattr(router, "_save", lambda _record: None)
    monkeypatch.setattr(router, "_kubernetes_resource_snapshot", lambda *_args, **_kwargs: _async_none())

    async def fake_create_run(_request):
        return {"id": "benchmark-1"}

    monkeypatch.setattr(router, "create_run", fake_create_run)

    async def fake_wait_for_benchmark(_benchmark_id):
        return {"status": "failed", "error": "boom"}

    monkeypatch.setattr(router, "_wait_for_benchmark", fake_wait_for_benchmark)

    guide_case = {"id": "guide-1", "kind": "guide", "status": "succeeded", "execution_id": "exec-1"}
    baseline_case = _baseline_case()
    workflow = _workflow(guide_case, baseline_case)

    with pytest.raises(ValueError, match="boom"):
        await router._execute_shared_pods_baseline_case(workflow, baseline_case)


async def _async_none():
    return None
