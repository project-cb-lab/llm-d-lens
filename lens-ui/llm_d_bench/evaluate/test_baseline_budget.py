"""Resource-budget parity for independently deployed PD comparison baselines."""

import importlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from llm_d_bench.deploy.contracts import DeployableConfiguration
from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter
from llm_d_bench.evaluate.models import EvaluationCreateRequest
from llm_d_bench.utils.artifacts import configuration_checksum

router = importlib.import_module("llm_d_bench.evaluate.router")


def pd_configuration(prefill_replicas=2, prefill_tp=1, decode_replicas=2, decode_tp=1):
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "prefill": {"replicaCount": prefill_replicas, "tensorParallelSize": prefill_tp},
        "decode": {"replicaCount": decode_replicas, "tensorParallelSize": decode_tp},
        "runtime": {"image": "example/vllm:test"},
    }
    return DeployableConfiguration(
        type="pd",
        provider_ref="pd-disaggregation",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )


@pytest.mark.parametrize(
    "baseline_type",
    [
        "direct-vllm",
        "router-neutral",
        "router-round-robin",
        "load-only",
        "affinity-only",
        "optimized-baseline",
    ],
)
def test_pd_baselines_default_to_total_card_budget(baseline_type):
    source = pd_configuration()
    original = deepcopy(source.content)
    baseline = router._baseline_configuration(source, baseline_type, ["plan"], "artifact")
    assert baseline.content["decode"] == {"replicaCount": 4, "tensorParallelSize": 1}
    assert "prefill" not in baseline.content
    assert source.content == original
    assert baseline.checksum == configuration_checksum(baseline.content)


@pytest.mark.parametrize(
    ("shape", "parameters", "replicas", "tp"),
    [
        ((1, 4, 2, 2), {}, 4, 2),
        ((2, 1, 2, 1), {"tensor_parallel_size": 2}, 2, 2),
        ((2, 1, 2, 1), {"max_model_len": 8192}, 4, 1),
        ((2, 1, 2, 1), {"replicas": None}, 4, 1),
        ((2, 1, 2, 1), {"replicas": 2}, 2, 1),
        ((1, 1, 1, 2), {"replicas": 1, "tensor_parallel_size": 2}, 1, 2),
    ],
)
def test_pd_budget_uses_effective_tp_and_honors_explicit_replicas(shape, parameters, replicas, tp):
    baseline = router._baseline_configuration(
        pd_configuration(*shape),
        "direct-vllm",
        ["plan"],
        "artifact",
        parameters,
    )
    assert baseline.content["decode"]["replicaCount"] == replicas
    assert baseline.content["decode"]["tensorParallelSize"] == tp


@pytest.mark.parametrize(
    ("shape", "parameters"),
    [
        ((1, 1, 1, 2), {}),  # Three cards cannot be divided into TP=2 replicas.
        ((2, 1, 2, 1), {"tensor_parallel_size": 8}),
        ((32, 1, 32, 1), {}),  # Derived replicas must respect the API's 32-replica limit.
    ],
)
def test_pd_budget_rejects_unrepresentable_automatic_replicas(shape, parameters):
    with pytest.raises(ValueError, match="GPU budget"):
        router._baseline_configuration(
            pd_configuration(*shape),
            "direct-vllm",
            ["plan"],
            "artifact",
            parameters,
        )


def test_non_pd_baseline_keeps_decode_replica_default():
    source = pd_configuration().model_copy(update={"provider_ref": "optimized-baseline"})
    baseline = router._baseline_configuration(source, "direct-vllm", ["plan"], "artifact")
    assert baseline.content["decode"]["replicaCount"] == 2


def test_pd_baseline_manifest_requests_four_single_card_replicas(monkeypatch):
    monkeypatch.setattr("llm_d_bench.deploy.providers.baseline_vllm.gpu_device_selectors", lambda: [])
    baseline = router._baseline_configuration(pd_configuration(), "direct-vllm", ["plan"], "artifact")
    resources = BaselineVllmAdapter._resources(BaselineVllmAdapter._parameters(baseline.content))
    deployment = next(resource for resource in resources if resource["kind"] == "Deployment")
    claim = next(resource for resource in resources if resource["kind"] == "ResourceClaimTemplate")
    assert deployment["spec"]["replicas"] == 4
    assert claim["spec"]["spec"]["devices"]["requests"][0]["exactly"]["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [(2, 1, 2, 1), (1, 1, 1, 2)])
async def test_create_evaluation_saves_matched_budget_or_rejects_before_queueing(monkeypatch, shape):
    artifact_id = "00000000-0000-4000-8000-000000000001"
    request = EvaluationCreateRequest(
        cluster_session_id="00000000-0000-4000-8000-000000000002",
        benchmark_plans=[{"id": "plan", "configuration_artifact_id": artifact_id}],
    )
    monkeypatch.setattr(router, "deployment_runtime_overrides", lambda *_: {})
    monkeypatch.setattr(router, "require_active_session", lambda *_: object())
    monkeypatch.setattr(router, "cluster_deployment_source", lambda *_: {})
    monkeypatch.setattr(router, "_cluster_benchmark_runtime", lambda *_: {})
    monkeypatch.setattr(router, "_validate_evaluation_capacity", lambda *_: None)
    monkeypatch.setattr(
        router,
        "get_configuration_artifact",
        lambda *_: SimpleNamespace(
            deployable_configuration=pd_configuration(*shape),
        ),
    )

    async def execute_noop(_workflow_id):
        pass

    tasks = {}
    monkeypatch.setattr(router, "_execute_evaluation", execute_noop)
    monkeypatch.setattr(router, "_workflow_tasks", tasks)
    saved = []
    monkeypatch.setattr(router, "_save", saved.append)
    if shape == (2, 1, 2, 1):
        workflow = await router.create_evaluation(request)
        await tasks[workflow["id"]]
        assert saved == [workflow]
        guide, baseline = workflow["cases"]
        assert guide["deployment_configuration"]["content"]["decode"]["replicaCount"] == 2
        assert baseline["deployment_configuration"]["content"]["decode"]["replicaCount"] == 4
        return
    with pytest.raises(HTTPException) as error:
        await router.create_evaluation(request)
    assert error.value.status_code == 409
    assert "GPU budget" in error.value.detail
    assert saved == []
    assert tasks == {}


def test_openapi_describes_pd_baseline_budget_defaults():
    app = FastAPI()
    app.include_router(router.router)
    schema = app.openapi()
    operation = next(
        path["post"]
        for path in schema["paths"].values()
        if path.get("post", {}).get("operationId") == "create_evaluation"
    )
    assert "total prefill plus decode GPU budget" in operation["description"]
    assert "409" in operation["description"]
    parameters = schema["components"]["schemas"]["ComparisonCoreParameters"]["properties"]
    assert "effective baseline TP" in parameters["replicas"]["description"]
    assert "total P+D GPU budget" in parameters["tensor_parallel_size"]["description"]
