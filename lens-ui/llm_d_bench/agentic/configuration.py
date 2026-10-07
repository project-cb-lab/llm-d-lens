"""Map bounded Agentic candidates to Deploy's existing configuration contract."""

from __future__ import annotations

from llm_d_bench.configuration.models import ModelSecretConfiguration
from llm_d_bench.deploy.contracts import DeployableConfiguration
from llm_d_bench.deploy.standard_kubernetes_service import (
    StandardKubernetesServiceRequest,
    build_standard_kubernetes_service_configuration,
)
from llm_d_bench.utils.artifacts import configuration_checksum

from .planner import PlannedCandidate

_XPU_IMAGE = "ghcr.io/llm-d/llm-d-xpu:v0.9.0"


def build_agentic_configuration(
    request: StandardKubernetesServiceRequest,
    candidate: PlannedCandidate,
    model_secret: ModelSecretConfiguration | None = None,
) -> DeployableConfiguration:
    """Create a provider-owned configuration without rendering or deploying it."""
    selected_request = request.model_copy(
        update={
            "replicas": candidate.replicas,
            "tensor_parallel_size": candidate.tensor_parallel_size,
        }
    )
    if candidate.provider_ref == "baseline-vllm":
        configuration = build_standard_kubernetes_service_configuration(selected_request)
        if model_secret is not None and model_secret.mode != "none":
            content = {
                **configuration.content,
                "modelSecret": model_secret.model_dump(mode="json", by_alias=True),
            }
            return configuration.model_copy(
                update={
                    "content": content,
                    "checksum": configuration_checksum(content),
                }
            )
        return configuration

    from llm_d_bench.deploy.providers.hardware_profile import runtime_image

    runtime = {
        "image": runtime_image(_XPU_IMAGE),
        "imageMode": "use-upstream-image",
        "modelSource": "auto-cache",
    }
    if request.storage_type == "local-cache":
        runtime["mountPath"] = request.model_cache_path
    elif request.storage_type == "model-cache":
        runtime["storageVolumeId"] = request.storage_volume_id
    else:
        runtime["pvcName"] = request.pvc_name

    decode = {
        "replicaCount": candidate.replicas,
        "tensorParallelSize": candidate.tensor_parallel_size,
        "maxModelLen": request.max_model_len,
        "gpuMemoryUtilization": request.gpu_memory_utilization,
        "maxNumSeqs": request.max_num_seqs,
    }
    content = {
        "model": {"name": request.model},
        "decode": decode,
        "runtime": runtime,
        "customParameters": [
            {
                "target": "decode",
                "kind": "argument",
                "name": "gpu-memory-utilization",
                "value": str(request.gpu_memory_utilization),
            },
            {
                "target": "decode",
                "kind": "argument",
                "name": "max-num-batched-tokens",
                "value": str(request.max_num_batched_tokens),
            },
            *[
                {"target": "decode", "kind": "argument", "name": argument["name"], "value": argument["value"]}
                for argument in request.vllm_arguments
            ],
        ],
    }
    # Agentic plans must carry the same credential policy as the selected
    # Model Cache entry.  Cache readiness only proves that model files exist;
    # llm-d still requires its deployment Secret, and omitting this field
    # silently degrades the downstream policy to ``mode=none``.
    if model_secret is not None and model_secret.mode != "none":
        content["modelSecret"] = model_secret.model_dump(mode="json", by_alias=True)
    configuration_type = "baseline"
    if candidate.provider_ref == "pd-disaggregation":
        configuration_type = "pd"
        content["prefill"] = {
            "replicaCount": candidate.prefill_replicas,
            "tensorParallelSize": candidate.prefill_tensor_parallel_size,
            "maxModelLen": request.max_model_len,
        }
        content["guideVariant"] = candidate.guide_variant or "vllm"
    elif candidate.provider_ref == "tiered-prefix-cache":
        configuration_type = "tiered_cache"
        content["cache"] = {"variant": candidate.guide_variant or "native/cpu/base"}
        content["guideVariant"] = candidate.guide_variant or "native/cpu/base"
    elif candidate.provider_ref == "precise-prefix-cache-routing":
        content["router"] = {}

    return DeployableConfiguration(
        type=configuration_type,
        format="helm",
        content=content,
        provider_ref=candidate.provider_ref,
        checksum=configuration_checksum(content),
        provenance={
            "source": "agentic-deployment.v1",
            "deployment_name": request.deployment_name,
            "description": request.description.strip(),
            "agentic_candidate_id": candidate.id,
        },
    )
