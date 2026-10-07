"""Lens-supported llm-d component versions (single source of truth).

The pinned versions live in ``llm_d_stack.yaml`` next to this module. Every
component that installs or references an llm-d artifact reads them from here so
no provider, planner or profile hardcodes a component version (see
``docs/design/llm-d-stack-profile-design.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml

_PROFILE_NAME = "llm_d_stack.yaml"

ROUTER_STANDALONE_CHART = "oci://ghcr.io/llm-d/charts/llm-d-router-standalone"
ROUTER_EPP_IMAGE_REPOSITORY = "ghcr.io/llm-d/llm-d-router-endpoint-picker"
ROUTER_PD_SIDECAR_IMAGE_REPOSITORY = "ghcr.io/llm-d/llm-d-router-disagg-sidecar"
INFERENCE_PAYLOAD_PROCESSOR_CHART = "oci://ghcr.io/llm-d/charts/payload-processor"
INFERENCE_PAYLOAD_PROCESSOR_IMAGE_REPOSITORY = "ghcr.io/llm-d/llm-d-inference-payload-processor"


@dataclass(frozen=True)
class GatewayProviderVersions:
    istio: str
    envoy_gateway: str
    envoy_ai_gateway: str
    agentgateway: str

    def for_provider(self, provider: str) -> str | None:
        """Resolve a gateway provider key (``istio``, ``envoy-ai-gateway``, ...)."""
        return getattr(self, provider.replace("-", "_"), None)


@dataclass(frozen=True)
class LlmdStack:
    llm_d: str
    min_k8s_version: str
    llm_d_router: str
    llm_d_benchmark: str
    llm_d_inference_payload_processor: str
    k8s_gateway_api: str
    k8s_gateway_api_inference_extension: str
    gateway_providers: GatewayProviderVersions


@lru_cache(maxsize=1)
def stack() -> LlmdStack:
    """Load and cache the pinned stack profile."""
    payload = yaml.safe_load(files("llm_d_bench.versions").joinpath(_PROFILE_NAME).read_text(encoding="utf-8"))
    providers = payload.get("gateway_providers") or {}
    return LlmdStack(
        llm_d=str(payload["llm_d"]),
        min_k8s_version=str(payload["min_k8s_version"]),
        llm_d_router=str(payload["llm_d_router"]),
        llm_d_benchmark=str(payload["llm_d_benchmark"]),
        llm_d_inference_payload_processor=str(payload["llm_d_inference_payload_processor"]),
        k8s_gateway_api=str(payload["k8s_gateway_api"]),
        k8s_gateway_api_inference_extension=str(payload["k8s_gateway_api_inference_extension"]),
        gateway_providers=GatewayProviderVersions(
            istio=str(providers["istio"]),
            envoy_gateway=str(providers["envoy_gateway"]),
            envoy_ai_gateway=str(providers["envoy_ai_gateway"]),
            agentgateway=str(providers["agentgateway"]),
        ),
    )


def llm_d_version() -> str:
    return stack().llm_d


def min_k8s_version() -> str:
    return stack().min_k8s_version


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = []
    for item in str(value).strip().lstrip("v").split(".")[:3]:
        digits = "".join(ch for ch in item if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    parts += [0] * (3 - len(parts))
    return (parts[0], parts[1], parts[2])


def k8s_version_supports(version: str) -> bool:
    """True when ``version`` is at least the profile's minimum Kubernetes version."""
    return _version_tuple(version) >= _version_tuple(stack().min_k8s_version)


def router_chart_version() -> str:
    return stack().llm_d_router


def router_chart() -> str:
    return ROUTER_STANDALONE_CHART


def router_epp_image() -> str:
    return f"{ROUTER_EPP_IMAGE_REPOSITORY}:{stack().llm_d_router}"


def router_pd_sidecar_image() -> str:
    return f"{ROUTER_PD_SIDECAR_IMAGE_REPOSITORY}:{stack().llm_d_router}"


def inference_payload_processor_chart() -> str:
    return INFERENCE_PAYLOAD_PROCESSOR_CHART


def inference_payload_processor_version() -> str:
    return stack().llm_d_inference_payload_processor


def inference_payload_processor_image() -> str:
    return f"{INFERENCE_PAYLOAD_PROCESSOR_IMAGE_REPOSITORY}:{stack().llm_d_inference_payload_processor}"


def gateway_provider_version(provider: str) -> str | None:
    return stack().gateway_providers.for_provider(provider)


def describe() -> dict[str, Any]:
    """The pinned stack inputs, for the API and UI."""
    current = stack()
    return {
        "llm_d": current.llm_d,
        "min_k8s_version": current.min_k8s_version,
        "llm_d_router": current.llm_d_router,
        "llm_d_benchmark": current.llm_d_benchmark,
        "llm_d_inference_payload_processor": current.llm_d_inference_payload_processor,
        "k8s_gateway_api": current.k8s_gateway_api,
        "k8s_gateway_api_inference_extension": current.k8s_gateway_api_inference_extension,
        "gateway_providers": {
            "istio": current.gateway_providers.istio,
            "envoy_gateway": current.gateway_providers.envoy_gateway,
            "envoy_ai_gateway": current.gateway_providers.envoy_ai_gateway,
            "agentgateway": current.gateway_providers.agentgateway,
        },
    }
