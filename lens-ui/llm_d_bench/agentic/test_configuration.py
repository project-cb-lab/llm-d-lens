"""Tests for mapping Agentic candidates to existing Deploy configurations."""

import pytest

from llm_d_bench.agentic.configuration import build_agentic_configuration
from llm_d_bench.agentic.planner import PlannedCandidate
from llm_d_bench.configuration.models import ModelSecretConfiguration
from llm_d_bench.deploy.configuration_adapter import DeploymentConfigurationPlanner
from llm_d_bench.deploy.standard_kubernetes_service import StandardKubernetesServiceRequest


@pytest.mark.parametrize(
    ("provider_ref", "configuration_type"),
    [
        ("optimized-baseline", "baseline"),
        ("pd-disaggregation", "pd"),
        ("tiered-prefix-cache", "tiered_cache"),
        ("precise-prefix-cache-routing", "baseline"),
    ],
)
def test_registered_guide_candidate_maps_to_deployable_configuration(provider_ref, configuration_type):
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-8B",
        deployment_name="agentic",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
    )
    candidate = PlannedCandidate(
        id=f"{provider_ref}-tp1-r1",
        provider_ref=provider_ref,
        replicas=1,
        tensor_parallel_size=1,
        prefill_replicas=1,
        prefill_tensor_parallel_size=1,
        guide_variant="native/cpu/base" if provider_ref == "tiered-prefix-cache" else "vllm",
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=2 if provider_ref == "pd-disaggregation" else 1,
        deployable=True,
    )

    configuration = build_agentic_configuration(request, candidate)

    assert configuration.provider_ref == provider_ref
    assert configuration.type == configuration_type
    assert configuration.content["decode"]["tensorParallelSize"] == 1
    assert configuration.content["decode"]["gpuMemoryUtilization"] == 0.9
    assert "modelSecret" not in configuration.content
    cases = DeploymentConfigurationPlanner().plan([configuration], run_id="run-1")
    assert cases[0].provider_ref == provider_ref
    if provider_ref == "pd-disaggregation":
        assert configuration.content["prefill"]["replicaCount"] == 1
    if provider_ref == "tiered-prefix-cache":
        assert configuration.content["guideVariant"] == "native/cpu/base"


def test_runtime_vllm_parameters_override_candidate_placeholders_after_approval():
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-8B",
        deployment_name="agentic",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=32768,
        gpu_memory_utilization=0.72,
        max_num_seqs=64,
        max_num_batched_tokens=4096,
    )
    candidate = PlannedCandidate(
        id="pd-disaggregation-p1-d1-tp1",
        provider_ref="pd-disaggregation",
        replicas=1,
        tensor_parallel_size=1,
        prefill_replicas=1,
        prefill_tensor_parallel_size=1,
        guide_variant="vllm",
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=2,
        deployable=True,
    )

    configuration = build_agentic_configuration(request, candidate)

    assert configuration.content["decode"]["maxModelLen"] == 32768
    assert configuration.content["decode"]["gpuMemoryUtilization"] == 0.72
    assert configuration.content["prefill"]["maxModelLen"] == 32768
    parameters = {item["name"]: item["value"] for item in configuration.content["customParameters"]}
    assert parameters["gpu-memory-utilization"] == "0.72"
    assert parameters["max-num-batched-tokens"] == "4096"


def test_agentic_configuration_propagates_model_cache_host_token_policy():
    """Guard the planning-to-deployment boundary that originally dropped credentials."""
    request = StandardKubernetesServiceRequest(
        model="google/gemma-4-12B-it-qat-w4a16-ct",
        deployment_name="agentic",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
    )
    candidate = PlannedCandidate(
        id="optimized-baseline-tp1-r1",
        provider_ref="optimized-baseline",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    configuration = build_agentic_configuration(
        request,
        candidate,
        ModelSecretConfiguration(mode="host"),
    )
    case = DeploymentConfigurationPlanner().plan([configuration], run_id="run-1")[0]

    assert configuration.content["modelSecret"] == {
        "mode": "host",
        "sourceNamespace": None,
        "sourceName": None,
    }
    assert case.create_request.deployment_policy.value["model_secret"]["mode"] == "host"


@pytest.mark.parametrize(
    "model_secret",
    [
        ModelSecretConfiguration(mode="host"),
        ModelSecretConfiguration(
            mode="existing-secret",
            sourceNamespace="models",
            sourceName="hf-token",
        ),
    ],
    ids=["host", "existing-secret"],
)
def test_agentic_baseline_vllm_propagates_model_secret_policy(model_secret):
    """Baseline candidates must retain the Model Cache credential policy."""
    request = StandardKubernetesServiceRequest(
        model="Qwen/Qwen3-8B",
        deployment_name="agentic",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
    )
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        provider_ref="baseline-vllm",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    configuration = build_agentic_configuration(request, candidate, model_secret)
    case = DeploymentConfigurationPlanner().plan([configuration], run_id="run-1")[0]

    assert configuration.content["modelSecret"] == model_secret.model_dump(mode="json", by_alias=True)
    assert case.create_request.deployment_policy.value["model_secret"] == model_secret.model_dump(
        mode="json",
        by_alias=True,
    )
