"""Tests for Evaluate deployment benchmark support."""

import importlib
from types import SimpleNamespace

import pytest

from llm_d_bench.deploy.contracts import DeployableConfiguration
from llm_d_bench.evaluate.models import EvaluationCreateRequest
from llm_d_bench.utils.artifacts import configuration_checksum

router = importlib.import_module("llm_d_bench.evaluate.router")


def test_xpumd_metrics_use_compute_engine_utilization():
    metrics = router._parse_xpumd_metrics(
        "\n".join(
            [
                'hw_gpu_utilization_ratio{pci_bdf="0000:01:00.0",hw_gpu_task="all"} 0.125',
                'hw_gpu_utilization_ratio{pci_bdf="0000:01:00.0",hw_gpu_task="compute-all"} 1',
                'hw_gpu_utilization_ratio{pci_bdf="0000:01:00.0",hw_gpu_task="copy-all"} 0',
            ]
        )
    )

    assert metrics["average_utilization_ratio"] == 1
    assert metrics["maximum_utilization_ratio"] == 1
    assert metrics["devices"][0]["utilization_ratio"] == 1


@pytest.mark.parametrize(
    ("baseline_type", "provider_ref", "router_profile"),
    [
        ("direct-vllm", "baseline-vllm", None),
        ("router-neutral", "optimized-baseline", "router-neutral"),
        ("optimized-baseline", "optimized-baseline", "optimized-baseline"),
    ],
)
def test_explicit_baseline_types_select_expected_provider(baseline_type, provider_ref, router_profile):
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 2, "tensorParallelSize": 1},
        "runtime": {"image": "example/vllm:test", "imageMode": "use-upstream-image"},
    }
    source = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="manifest",
        content=content,
        checksum=configuration_checksum(content),
    )

    baseline = router._baseline_configuration(source, baseline_type, ["workload"], "artifact")

    assert baseline.provider_ref == provider_ref
    assert baseline.content.get("routerProfile") == router_profile
    assert baseline.content["decode"] == content["decode"]
    assert baseline.checksum == configuration_checksum(baseline.content)


def test_comparison_core_parameters_override_generated_baseline():
    content = {
        "model": {"name": "Qwen/Qwen3-8B"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1, "maxModelLen": 20480},
        "runtime": {"image": "example/vllm:test", "imageMode": "use-upstream-image"},
        "customParameters": [{"target": "decode", "kind": "argument", "name": "block-size", "value": "16"}],
    }
    source = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="manifest",
        content=content,
        checksum=configuration_checksum(content),
    )

    baseline = router._baseline_configuration(
        source,
        "direct-vllm",
        ["workload"],
        "artifact",
        {
            "replicas": 2,
            "tensor_parallel_size": 1,
            "max_model_len": 16384,
            "max_num_seqs": 64,
            "gpu_memory_utilization": 0.85,
            "block_size": 64,
            "max_num_batched_tokens": 2048,
        },
    )

    assert baseline.content["decode"] == {
        "replicaCount": 2,
        "tensorParallelSize": 1,
        "maxModelLen": 16384,
        "maxNumSeqs": 64,
    }
    arguments = {item["name"]: item["value"] for item in baseline.content["customParameters"]}
    assert arguments == {
        "gpu-memory-utilization": "0.85",
        "block-size": "64",
        "max-num-batched-tokens": "2048",
    }
    assert baseline.checksum == configuration_checksum(baseline.content)


def test_evaluation_case_links_candidate_to_multiple_baselines():
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1},
        "runtime": {"image": "example/vllm:test", "imageMode": "use-upstream-image"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="manifest",
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
                "baseline_types": ["direct-vllm", "router-neutral", "optimized-baseline"],
            }
        ],
    )

    cases = router._evaluation_cases(request, {artifact_id: SimpleNamespace(deployable_configuration=configuration)})

    assert cases[0]["kind"] == "guide"
    assert cases[0]["baseline_case_ids"] == ["baseline-1", "baseline-2", "baseline-3"]
    assert [case["baseline_type"] for case in cases[1:]] == ["direct-vllm", "router-neutral", "optimized-baseline"]


@pytest.mark.parametrize(
    "provider_ref",
    ["optimized-baseline", "pd-disaggregation", "tiered-prefix-cache", "precise-prefix-cache-routing"],
)
def test_suite_scenarios_expand_for_every_supported_guide(provider_ref):
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1},
        "runtime": {"image": "example/vllm:test"},
    }
    if provider_ref == "pd-disaggregation":
        content["prefill"] = {"replicaCount": 1, "tensorParallelSize": 1}
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref=provider_ref,
        format="manifest",
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
                "include_baseline": False,
                "scenarios": [
                    {"id": "smoke", "name": "Smoke", "benchmark": {"matrix": [{"isl": 128, "osl": 64}]}},
                    {"id": "chat", "name": "Chat", "benchmark": {"matrix": [{"isl": 256, "osl": 128}]}},
                ],
            }
        ],
    )

    cases = router._evaluation_cases(request, {artifact_id: SimpleNamespace(deployable_configuration=configuration)})

    assert [case["id"] for case in cases] == ["guide-1-1", "guide-1-2"]
    assert [case["scenario_id"] for case in cases] == ["smoke", "chat"]
    assert all(case["deployment_configuration"]["provider_ref"] == provider_ref for case in cases)


def test_suite_sla_evaluation_uses_requested_percentiles():
    result = router._sla_evaluation(
        {
            "throughput_tps": 1200,
            "success_rate": 99.5,
            "latency_distributions": {
                "ttft": {"p99_ms": 450},
                "tpot": {"p90_ms": 55},
            },
        },
        {
            "ttft_ms": 500,
            "ttft_percentile": "p99",
            "tpot_ms": 50,
            "tpot_percentile": "p90",
            "throughput_min_tps": 1000,
            "success_rate_min_percent": 99,
        },
    )

    assert result["met"] is False
    assert result["checks"]["ttft"]["met"] is True
    assert result["checks"]["tpot"]["met"] is False


def test_suite_scenarios_share_deployment_until_the_last_scenario():
    workflow = {
        "cases": [
            {"id": "guide-1-1", "kind": "guide", "status": "benchmarking", "deployment_group_id": "candidate:guide"},
            {
                "id": "guide-1-2",
                "kind": "guide",
                "status": "queued",
                "deployment_group_id": "candidate:guide",
                "deployment_run_id": None,
            },
            {
                "id": "guide-2-1",
                "kind": "guide",
                "status": "queued",
                "deployment_group_id": "other:guide",
                "deployment_run_id": None,
            },
        ]
    }

    first, second, other = workflow["cases"]
    router._share_suite_deployment(workflow, first, "deployment-1")

    assert second["deployment_run_id"] == "deployment-1"
    assert other["deployment_run_id"] is None
    assert router._has_pending_suite_scenario(workflow, first)
    second["status"] = "succeeded"
    assert not router._has_pending_suite_scenario(workflow, first)


def test_evaluation_capacity_rejects_model_that_cannot_fit_xpu(monkeypatch):
    monkeypatch.setenv("PRISM_ACCELERATOR_MEMORY_GIB", "24")
    content = {
        "model": {"name": "Qwen/Qwen3-32B"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1},
        "runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.8.0"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )

    with pytest.raises(ValueError, match="use TP>=4"):
        router._validate_evaluation_capacity(configuration)

    configuration.content["decode"]["tensorParallelSize"] = 4
    router._validate_evaluation_capacity(configuration)


def test_evaluation_capacity_rejects_more_cards_than_allowlisted(monkeypatch):
    monkeypatch.setenv("PRISM_ACCELERATOR_MEMORY_GIB", "24")
    monkeypatch.setenv("PRISM_GPU_PCI_ALLOWLIST", "gpu-1,gpu-2,gpu-3,gpu-4")
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 2, "tensorParallelSize": 4},
        "runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.8.0"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )

    with pytest.raises(ValueError, match="configuration requires 8 XPU cards, but only 4 are available to Prism"):
        router._validate_evaluation_capacity(configuration)


@pytest.mark.parametrize("preserve_deployment", [False, True])
def test_failed_benchmark_always_cleans_owned_deployment(preserve_deployment):
    assert router._should_cleanup_deployment(
        owns_deployment=True,
        benchmark_succeeded=False,
        case={"preserve_deployment": preserve_deployment},
    )


def test_successful_benchmark_keeps_deployment_only_when_requested():
    assert router._should_cleanup_deployment(
        owns_deployment=True, benchmark_succeeded=True, case={"preserve_deployment": False}
    )
    assert not router._should_cleanup_deployment(
        owns_deployment=True, benchmark_succeeded=True, case={"preserve_deployment": True}
    )


def test_evaluation_does_not_clean_external_deployment():
    assert not router._should_cleanup_deployment(
        owns_deployment=False, benchmark_succeeded=False, case={"preserve_deployment": False}
    )


@pytest.mark.asyncio
async def test_delete_terminal_endpoint_benchmark_removes_record_and_results(monkeypatch, tmp_path):
    run_id = "00000000-0000-4000-8000-000000000001"
    run = {"kind": "benchmark", "id": run_id, "status": "failed"}
    results = tmp_path / "results"
    output = results / run_id
    output.mkdir(parents=True)
    (output / "report.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(router, "_results_root", results)
    router._save(run)

    await router.delete_benchmark_run(run_id)

    assert router._get("benchmark", run_id) is None
    assert not output.exists()


@pytest.mark.asyncio
async def test_delete_active_endpoint_benchmark_is_rejected(monkeypatch):
    run_id = "00000000-0000-4000-8000-000000000002"
    monkeypatch.setattr(router, "_get", lambda _kind, _record_id: {"id": run_id, "status": "running"})

    with pytest.raises(router.HTTPException, match="must be cancelled") as raised:
        await router.delete_benchmark_run(run_id)

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_workflow_deletes_owned_deployments_and_records(monkeypatch, tmp_path):
    workflow_id = "00000000-0000-4000-8000-000000000021"
    workflow = {
        "kind": "workflow",
        "id": workflow_id,
        "status": "succeeded",
        "deployment_run_id": "legacy-deployment",
        "cases": [
            {
                "id": "00000000-0000-4000-8000-000000000031",
                "status": "succeeded",
                "deployment_run_id": "deployment-1",
                "evaluation_run_id": "00000000-0000-4000-8000-000000000011",
            },
            {
                "id": "00000000-0000-4000-8000-000000000032",
                "status": "succeeded",
                "deployment_run_id": "deployment-1",
                "evaluation_run_id": "00000000-0000-4000-8000-000000000012",
            },
        ],
    }
    deleted_deployments = []
    monkeypatch.setattr(router, "_results_root", tmp_path / "results")
    monkeypatch.setattr(router, "_records", lambda kind: [])

    class Worker:
        def __init__(self, run_id):
            self.run_id = run_id

        async def delete_run(self, run_id):
            assert run_id == self.run_id
            deleted_deployments.append(run_id)

    monkeypatch.setattr(router, "_store", SimpleNamespace(get_run=lambda _run_id: object()))
    monkeypatch.setattr(
        router,
        "deployment_run_manager",
        SimpleNamespace(worker_for_run=lambda run_id: Worker(run_id)),
    )
    router._save(workflow)
    router._save({"kind": "benchmark", "id": "00000000-0000-4000-8000-000000000011", "status": "failed"})
    router._save({"kind": "benchmark", "id": "00000000-0000-4000-8000-000000000012", "status": "failed"})

    await router.delete_workflow_run(workflow_id)

    assert set(deleted_deployments) == {"legacy-deployment", "deployment-1"}
    assert router._get("workflow", workflow_id) is None
    assert router._get("benchmark", "00000000-0000-4000-8000-000000000011") is None
    assert router._get("benchmark", "00000000-0000-4000-8000-000000000012") is None


@pytest.mark.asyncio
async def test_delete_workflow_keeps_records_when_deployment_cleanup_fails(monkeypatch, tmp_path):
    workflow_id = "00000000-0000-4000-8000-000000000022"
    workflow = {
        "kind": "workflow",
        "id": workflow_id,
        "status": "failed",
        "cases": [
            {
                "id": "00000000-0000-4000-8000-000000000033",
                "status": "failed",
                "deployment_run_id": "deployment-1",
                "evaluation_run_id": "00000000-0000-4000-8000-000000000011",
            }
        ],
    }

    class Worker:
        async def delete_run(self, _run_id):
            raise ValueError("reconnect the deployment cluster")

    router._save(workflow)
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_run=lambda _run_id: object()))
    monkeypatch.setattr(router, "deployment_run_manager", SimpleNamespace(worker_for_run=lambda _run_id: Worker()))

    with pytest.raises(router.HTTPException, match="reconnect the deployment cluster") as raised:
        await router.delete_workflow_run(workflow_id)

    assert raised.value.status_code == 409
    saved = router._get("workflow", workflow_id)
    assert saved is not None
    assert saved["id"] == workflow["id"]
    assert saved["status"] == workflow["status"]
    assert [case["id"] for case in saved["cases"]] == [case["id"] for case in workflow["cases"]]
    assert [case["status"] for case in saved["cases"]] == [case["status"] for case in workflow["cases"]]
    assert [case["deployment_run_id"] for case in saved["cases"]] == [
        case["deployment_run_id"] for case in workflow["cases"]
    ]


@pytest.mark.asyncio
async def test_benchmark_runtime_requires_cluster_source(monkeypatch, tmp_path):
    executable = tmp_path / ".venv" / "bin" / "llmdbenchmark"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setenv("LLM_D_BENCHMARK_EXECUTABLE", str(executable))
    monkeypatch.setenv("LLM_D_BENCHMARK_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="Software Versions"):
        await router._prepare_benchmark_runtime()


@pytest.mark.asyncio
async def test_benchmark_runtime_uses_cluster_checkout(tmp_path):
    executable = tmp_path / ".venv" / "bin" / "llmdbenchmark"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    assert await router._prepare_benchmark_runtime({"resolved_repository": str(tmp_path)}) == (
        str(executable),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_workflow_persists_deployment_failure_stage_before_clearing_active_case(monkeypatch):
    import copy

    workflow = {
        "kind": "workflow",
        "id": "stage-test",
        "status": "queued",
        "failed_stage": "benchmarking",
        "cases": [
            {"id": "case-1", "kind": "guide", "status": "queued", "failed_stage": "benchmarking"},
        ],
    }
    saved = []
    monkeypatch.setattr(router, "_get", lambda *_: workflow)
    monkeypatch.setattr(router, "_save", lambda value: saved.append(copy.deepcopy(value)))
    monkeypatch.setattr(router, "_ordered_evaluation_cases", lambda cases: cases)

    def invalid_configuration(*_):
        raise ValueError("deployment configuration rejected")

    monkeypatch.setattr(router, "_case_configuration", invalid_configuration)
    await router._execute_evaluation(workflow["id"])

    final = saved[-1]
    assert final["status"] == "failed"
    assert final["active_case_id"] is None
    assert final["failed_stage"] == "deploying"
    assert final["cases"][0]["failed_stage"] == "deploying"
    assert final["cases"][0]["error"] == "deployment configuration rejected"
    assert "failed_stage" not in saved[0]
    assert "failed_stage" not in saved[0]["cases"][0]


def test_each_configuration_can_select_only_ablations_and_keeps_its_own_comparisons():
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 2, "tensorParallelSize": 1},
        "runtime": {"image": "vllm:xpu"},
    }
    config = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )
    a = "00000000-0000-4000-8000-000000000001"
    b = "00000000-0000-4000-8000-000000000002"
    request = EvaluationCreateRequest(
        cluster_session_id=a,
        benchmark_plans=[
            {
                "id": "a",
                "configuration_artifact_id": a,
                "include_configuration": False,
                "baseline_types": ["load-only", "affinity-only"],
            },
            {"id": "b", "configuration_artifact_id": b, "include_configuration": True, "baseline_types": ["load-only"]},
        ],
    )
    cases = router._evaluation_cases(
        request,
        {a: SimpleNamespace(deployable_configuration=config), b: SimpleNamespace(deployable_configuration=config)},
    )
    assert len(cases) == 4
    assert [(c["kind"], c["benchmark_plan_id"]) for c in cases] == [
        ("guide", "b"),
        ("baseline", "a"),
        ("baseline", "a"),
        ("baseline", "b"),
    ]
    assert cases[0]["baseline_case_ids"] == [cases[-1]["id"]]


def test_router_ablations_preserve_published_source_and_cluster_for_retry():
    content = {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 2, "tensorParallelSize": 1},
        "runtime": {"image": "vllm:xpu"},
        "officialGuide": {"renderedManifest": "saved-manifest", "deploymentBundle": {"saved": True}},
        "guideSettings": {"routerValues": "saved-router"},
    }
    source = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="manifest",
        content=content,
        checksum=configuration_checksum(content),
        provenance={"cluster_ref": {"id": "original-cluster"}},
    )
    for kind in ["router-neutral", "affinity-only", "load-only"]:
        baseline = router._baseline_configuration(source, kind, ["plan-a"], "artifact-a")
        assert baseline.content["officialGuide"] == content["officialGuide"]
        assert baseline.content["guideSettings"] == content["guideSettings"]
        assert baseline.provenance["cluster_ref"]["id"] == "original-cluster"
        assert baseline.content["routerProfile"] == kind
        assert "routerProfile" not in source.content
    direct = router._baseline_configuration(source, "direct-vllm", ["plan-a"], "artifact-a")
    assert direct.provenance["cluster_ref"]["id"] == "original-cluster"
    assert "officialGuide" not in direct.content
