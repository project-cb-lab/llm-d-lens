"""Tests for configurable llm-d-benchmark sources."""

import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from llm_d_bench.evaluate.models import BenchmarkPlan, BenchmarkSource
from llm_d_bench.evaluate.router import (
    _cluster_benchmark_runtime,
    _execution_accelerator_profile,
    _harness_proxy_override,
    _install_helm_proxy_wrapper,
    _prepare_benchmark_storage,
)

evaluate_router = importlib.import_module("llm_d_bench.evaluate.router")


def test_benchmark_plan_normalizes_multiple_baselines_and_legacy_alias():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        baseline_types=["direct-vllm", "router-round-robin", "optimized-baseline"],
    )

    assert plan.baseline_types == ["direct-vllm", "router-neutral", "optimized-baseline"]
    assert plan.baseline_type == "direct-vllm"


def test_benchmark_plan_accepts_optimized_baseline_ablation_arms():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        baseline_types=["kubernetes-service", "load-only", "affinity-only"],
    )
    assert plan.baseline_types == ["kubernetes-service", "load-only", "affinity-only"]


def test_benchmark_plan_disables_all_baselines_when_not_included():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        include_baseline=False,
        baseline_types=["direct-vllm", "optimized-baseline"],
    )

    assert plan.baseline_types == []


def test_benchmark_plan_accepts_named_suite_scenarios_and_keeps_legacy_benchmark():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        benchmark={"workload": "legacy.yaml"},
        scenarios=[
            {
                "id": "interactive",
                "name": "Interactive chat",
                "benchmark": {
                    "matrix": [{"isl": 256, "osl": 128}],
                    "concurrency_stages": [{"concurrency": 8, "num_requests": 100}],
                },
                "sla_targets": {"ttft_ms": 500, "tpot_ms": 50, "success_rate_min_percent": 99},
            }
        ],
    )

    assert plan.benchmark.workload == "legacy.yaml"
    assert plan.scenarios[0].benchmark.matrix[0].isl == 256
    assert plan.scenarios[0].sla_targets.ttft_percentile == "p99"


def test_benchmark_plan_rejects_duplicate_suite_scenario_ids():
    with pytest.raises(ValidationError, match="scenario ids must be unique"):
        BenchmarkPlan(
            id="candidate",
            configuration_artifact_id="00000000-0000-4000-8000-000000000001",
            scenarios=[
                {"id": "same", "name": "First"},
                {"id": "same", "name": "Second"},
            ],
        )


def test_benchmark_source_accepts_https_repository_and_version():
    source = BenchmarkSource(
        repository="https://github.com/llm-d/llm-d-benchmark.git",
        revision="v1.2.3",
    )
    assert source.revision == "v1.2.3"


def test_benchmark_source_rejects_credentials_and_unsafe_revision():
    with pytest.raises(ValidationError):
        BenchmarkSource(repository="https://token@example.com/repo.git", revision="main")
    with pytest.raises(ValidationError):
        BenchmarkSource(repository="https://example.com/repo.git", revision="--upload-pack=bad")


def test_cluster_benchmark_runtime_uses_registered_checkout(monkeypatch, tmp_path):
    checkout = tmp_path / "llm-d-benchmark"
    checkout.mkdir()
    monkeypatch.setattr(
        evaluate_router,
        "require_active_session",
        lambda _session_id: SimpleNamespace(server_id="cluster-1"),
    )
    monkeypatch.setattr(
        evaluate_router.cluster_registry,
        "require_cluster",
        lambda cluster_id: SimpleNamespace(
            id=cluster_id,
            name="test-cluster",
            llm_d_benchmark_repo_path=str(checkout),
            llm_d_benchmark_ref="v0.4.0",
        ),
    )

    assert _cluster_benchmark_runtime("a" * 36) == {
        "resolved_repository": str(checkout),
        "ref": "v0.4.0",
        "resolved_from": "cluster-registry",
    }


def test_harness_proxy_environment_preserves_backend_cluster_bypass(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,10.112.228.229,10.233.0.0/18")
    monkeypatch.setenv("no_proxy", "localhost,10.112.0.0/16")
    environment = evaluate_router._harness_proxy_environment(
        {
            "http_proxy": "http://proxy.example:911",
            "https_proxy": "http://proxy.example:911",
            "no_proxy": "intel.com,.intel.com,localhost,127.0.0.1",
        }
    )

    assert environment["NO_PROXY"] == environment["no_proxy"]
    assert environment["NO_PROXY"].split(",") == [
        "intel.com",
        ".intel.com",
        "localhost",
        "127.0.0.1",
        "10.112.228.229",
        "10.233.0.0/18",
        "10.112.0.0/16",
        ".svc",
        ".cluster.local",
    ]
    assert environment["HTTP_PROXY"] == environment["http_proxy"] == "http://proxy.example:911"
    assert environment["HTTPS_PROXY"] == environment["https_proxy"] == "http://proxy.example:911"


@pytest.mark.parametrize("run_no_proxy", [None, "", " , localhost, localhost , "])
def test_harness_proxy_environment_normalizes_empty_and_duplicate_bypass(monkeypatch, run_no_proxy):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    environment = evaluate_router._harness_proxy_environment({"no_proxy": run_no_proxy})

    assert environment["NO_PROXY"] == "localhost,127.0.0.1,.svc,.cluster.local"
    assert environment["NO_PROXY"] == environment["no_proxy"]


def test_harness_proxy_environment_always_bypasses_kubeconfig_api_server(monkeypatch, tmp_path):
    """Regression test: an IP-based API server behind a corporate proxy must
    always be reachable, even when neither the run nor the backend's own
    NO_PROXY happen to include it (see the "warm-up run failed ... ProxyError
    ... Tunnel connection failed: 403 Forbidden" class of bug)."""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    kubeconfig = tmp_path / "kubeconfig.yaml"
    kubeconfig.write_text(
        "clusters:\n- cluster:\n    server: https://10.112.228.229:6443\n  name: cluster\n",
        encoding="utf-8",
    )

    environment = evaluate_router._harness_proxy_environment(
        {"http_proxy": "http://proxy.example:911", "https_proxy": "http://proxy.example:911"},
        kubeconfig_path=str(kubeconfig),
    )

    assert "10.112.228.229" in environment["NO_PROXY"].split(",")


def test_harness_proxy_environment_tolerates_missing_or_invalid_kubeconfig(tmp_path):
    assert evaluate_router._harness_proxy_environment({}, kubeconfig_path=None) is not None
    assert evaluate_router._harness_proxy_environment({}, kubeconfig_path=str(tmp_path / "missing.yaml")) is not None
    garbage = tmp_path / "garbage.yaml"
    garbage.write_text("not: [valid, yaml:", encoding="utf-8")
    assert evaluate_router._harness_proxy_environment({}, kubeconfig_path=str(garbage)) is not None


def test_helm_proxy_wrapper_scopes_proxy_to_helm(monkeypatch, tmp_path):
    helm = tmp_path / "system-bin" / "helm"
    helm.parent.mkdir()
    helm.write_text(
        f"#!{sys.executable}\nimport json, os\n"
        'print(json.dumps({k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}))\n',
        encoding="utf-8",
    )
    helm.chmod(0o700)
    environment = {"PATH": str(helm.parent)}

    _install_helm_proxy_wrapper(
        tmp_path / "output",
        environment,
        {"HTTPS_PROXY": "http://proxy.example:911", "NO_PROXY": "localhost"},
    )

    wrapper = tmp_path / "output" / ".bin" / "helm"
    assert wrapper.is_file()
    assert environment["PATH"].split(os.pathsep)[0] == str(wrapper.parent)
    completed = subprocess.run([str(wrapper), "version"], env=environment, capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {"HTTPS_PROXY": "http://proxy.example:911", "NO_PROXY": "localhost"}
    assert "HTTPS_PROXY" not in environment


def test_harness_proxy_override_injects_proxy_values_directly():
    override = _harness_proxy_override(
        {
            "HTTP_PROXY": "http://proxy.example:911",
            "HTTPS_PROXY": "http://proxy.example:911",
            "NO_PROXY": "localhost,.svc",
            "http_proxy": "ignored-duplicate",
        }
    )

    assert override is not None
    assert json.loads(override.split("=", 1)[1]) == [
        {"name": "HTTP_PROXY", "value": "http://proxy.example:911"},
        {"name": "HTTPS_PROXY", "value": "http://proxy.example:911"},
        {"name": "NO_PROXY", "value": "localhost,.svc"},
    ]


def test_accelerator_profile_is_derived_from_deployment_manifest():
    execution = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                provider_ref="optimized-baseline",
                content=json.dumps(
                    {
                        "officialGuide": {
                            "renderedManifest": "deviceClassName: gpu.intel.com\nnetworkDeviceClassName: dra.net\n"
                        }
                    }
                ),
            )
        ]
    )

    assert _execution_accelerator_profile(execution) == "intel-xpu"
    assert _execution_accelerator_profile(execution, "custom.xpu.example") == "custom.xpu.example"


def test_accelerator_profile_is_derived_for_generated_xpu_baseline():
    execution = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                provider_ref="baseline-vllm",
                content=json.dumps({"runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.8.0"}}),
            )
        ]
    )

    assert _execution_accelerator_profile(execution) == "intel-xpu"


@pytest.mark.asyncio
async def test_benchmark_storage_uses_cluster_default(monkeypatch):
    async def fake_kubectl(_environment, *arguments, stdin=None):
        assert arguments == ("get", "storageclass", "-o", "json")
        assert stdin is None
        return {
            "items": [
                {"metadata": {"name": "shared", "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}}
            ]
        }

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    assert await _prepare_benchmark_storage("run", "namespace", {}, None) == ("shared", None)


@pytest.mark.asyncio
async def test_benchmark_storage_falls_back_without_storage_class_or_target_node(monkeypatch):
    calls = []

    async def fake_kubectl(_environment, *arguments, stdin=None):
        calls.append((arguments, stdin))
        return {"items": []} if arguments[0] == "get" else {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    monkeypatch.delenv("PRISM_K8S_TARGET_NODE", raising=False)

    storage_class, volume = await _prepare_benchmark_storage(
        "00000000-0000-4000-8000-000000000002", "deployment-namespace", {}, None
    )

    manifest = json.loads(calls[1][1])
    persistent_volume = manifest["items"][1]
    assert storage_class == "prism-benchmark-local"
    assert volume == "prism-benchmark-00000000-0000-4000-8000-000000000002"
    assert "nodeAffinity" not in persistent_volume["spec"]


@pytest.mark.asyncio
async def test_benchmark_storage_creates_prebound_target_node_volume(monkeypatch):
    calls = []

    async def fake_kubectl(_environment, *arguments, stdin=None):
        calls.append((arguments, stdin))
        return {"items": []} if arguments[0] == "get" else {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "smc-18")

    storage_class, volume = await _prepare_benchmark_storage(
        "00000000-0000-4000-8000-000000000003", "deployment-namespace", {}, None
    )

    manifest = json.loads(calls[1][1])
    persistent_volume = manifest["items"][1]
    assert storage_class == "prism-benchmark-local"
    assert volume == "prism-benchmark-00000000-0000-4000-8000-000000000003"
    assert persistent_volume["spec"]["claimRef"] == {
        "namespace": "deployment-namespace", "name": "workload-pvc"
    }
    assert persistent_volume["spec"]["accessModes"] == ["ReadWriteOnce", "ReadWriteMany"]
    assert persistent_volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0][
        "matchExpressions"
    ][0]["values"] == ["smc-18"]


@pytest.mark.asyncio
async def test_benchmark_storage_cleanup_deletes_volume_when_claim_cleanup_fails(monkeypatch):
    calls = []

    async def fake_kubectl(_environment, *arguments, stdin=None):
        calls.append(arguments)
        if arguments[1] == "pvc":
            raise RuntimeError("namespace is gone")
        return {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)

    await evaluate_router._cleanup_benchmark_storage("deleted-namespace", "benchmark-volume", {})

    assert calls[-1][:3] == ("delete", "persistentvolume", "benchmark-volume")


def test_plan_can_select_all_routing_combinations_without_full_guide():
    plan = BenchmarkPlan(
        id="ablation",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        include_configuration=False,
        baseline_types=["router-neutral", "affinity-only", "load-only", "direct-vllm"],
    )
    assert plan.include_configuration is False
    assert len(plan.baseline_types) == 4


def test_plan_rejects_empty_selection_and_missing_same_pod_candidate():
    for extra in ({"include_baseline": False}, {"baseline_types": ["kubernetes-service"]}):
        with pytest.raises(ValidationError):
            BenchmarkPlan(
                id="empty",
                configuration_artifact_id="00000000-0000-4000-8000-000000000001",
                include_configuration=False,
                **extra,
            )


@pytest.mark.parametrize("value", [0, -1, 513, 1.5, True, "32", None])
def test_harness_memory_rejects_invalid_budgets(value):
    from llm_d_bench.evaluate.models import BenchmarkSpec

    for model, kwargs in [
        (BenchmarkSpec, {}),
        (evaluate_router.EvaluateRunRequest, {"deployment_execution_id": "exec"}),
    ]:
        with pytest.raises(ValidationError):
            model(**kwargs, harness_memory_gib=value)


def test_harness_memory_defaults_and_explicit_budget():
    from llm_d_bench.evaluate.models import BenchmarkSpec

    assert BenchmarkSpec().harness_memory_gib == 8
    assert evaluate_router.EvaluateRunRequest(deployment_execution_id="exec").harness_memory_gib == 8
    assert BenchmarkSpec(harness_memory_gib=64).model_dump()["harness_memory_gib"] == 64


@pytest.mark.parametrize("separate_limit", [False, True])
def test_harness_budget_overrides_xpu_request_and_limit(tmp_path, separate_limit):
    import yaml
    from jinja2 import Template

    # Both generations of the upstream Pod template must yield a valid request
    # <= limit, including when the XPU overlay explicitly sets memoryLimit=8Gi.
    limit = (
        "harness.resources.memoryLimit | default(harness.resources.memory, true)"
        if separate_limit
        else "harness.resources.memory"
    )
    source = (
        "resources:\n  requests:\n    memory: {{ harness.resources.memory }}\n  limits:\n    memory: {{ "
        + limit
        + " }}\n"
    )
    template = tmp_path / "config/templates/jinja/20_harness_pod.yaml.j2"
    template.parent.mkdir(parents=True)
    template.write_text(source)
    values = {"harness": {"resources": {"memory": "8Gi", "memoryLimit": "8Gi"}}}
    for override in evaluate_router._harness_resource_overrides({"harness_memory_gib": 64}, tmp_path):
        key, value = override.split("=", 1)
        values["harness"]["resources"][key.split(".")[-1]] = value
    rendered = yaml.safe_load(Template(source).render(**values))["resources"]
    assert rendered["requests"]["memory"] == "64Gi"
    assert rendered["limits"]["memory"] == "64Gi"


@pytest.mark.parametrize(
    "message",
    [
        "modelserver (terminated: OOMKilled, exit_code=137)",
        "inference-perf-test/harness (terminated: Error, exit_code=137)",
        "Connection refused",
    ],
)
def test_harness_oom_diagnostic_does_not_misclassify_other_failures(message):
    assert evaluate_router._benchmark_failure_message(message, {}) == message
