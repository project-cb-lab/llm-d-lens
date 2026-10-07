"""Stable deployment provider capabilities shared by Configuration and Evaluate."""

from __future__ import annotations



def shared_prefix_routing_workload() -> dict:
    """Fresh routing-comparison workload; provider goals and baselines stay local."""
    return {
        "num_groups": 150, "num_prompts_per_group": 5,
        "system_prompt_len": 6000, "question_len": 1200, "output_len": 1000,
        "enable_multi_turn_chat": False, "interval": 60,
        "stages": [{"rate": rate, "duration": 60} for rate in (3, 10, 20, 30, 40, 49, 55, 60)],
    }

# This flag declares whether a provider exposes a second endpoint on the same warmed pods
# for a routing-bypassing Kubernetes Service comparison. Evaluate consumes this registry;
# adding a provider capability must not require provider-name checks in the router or UI.
_PROVIDER_CAPABILITIES = {
    "optimized-baseline": {
        "label": "Optimized baseline",
        "variants": [],
        "model_servers": ["vllm"],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": True,
        "evaluation": {
            "default_variant": "",
            "required_baselines": ["kubernetes-service", "load-only", "affinity-only"],
            "experiment_variable": "routing policy",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full routing evaluation",
                    "recommended": True,
                    "description": (
                        "Kubernetes RR, load-only, affinity-only, and full routing arms under a shared-prefix "
                        "saturation sweep."
                    ),
                    "scenarios": [
                        {
                            "id": "shared-prefix",
                            "name": "Shared-prefix routing performance",
                            "description": (
                                "Increase request rate for repeated long prompts and measure throughput, TTFT, and "
                                "prefix locality against the selected references."
                            ),
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload()},
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": (
                        "Run the shared-prefix workload across RR, load-only, affinity-only, and full optimized "
                        "routing."
                    ),
                    "scenario_ids": ["shared-prefix"],
                },
            ],
            "comparison_arms": ["kubernetes-service", "load-only", "affinity-only", "optimized-baseline"],
            "evidence": ["Prefix locality", "Per-endpoint token load", "Queue growth", "TTFT and SLO capacity"],
            "validation": [
                "Expected EPP plugins",
                "Endpoint discovery",
                "Prefill calibration",
                "Router and model metrics",
            ],
            "recommended_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload()},
        },
    },
    "pd-disaggregation": {
        "label": "Prefill/decode disaggregation",
        "variants": ["vllm", "vllm-rdma"],
        "model_servers": ["vllm", "vllm-rdma"],
        "supports_pd": True,
        "supports_kubernetes_service_baseline": False,
        "evaluation": {
            "default_variant": "vllm",
            "required_baselines": ["direct-vllm"],
            "experiment_variable": "serving topology",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full P/D evaluation",
                    "recommended": True,
                    "description": (
                        "Resource-parity aggregate control plus long-input, negative-control, and geometry workloads."
                    ),
                    "scenarios": [
                        {
                            "id": "long-input",
                            "name": "P/D long-prompt performance",
                            "description": (
                                "Run long-input requests at increasing concurrency to measure the TTFT and throughput "
                                "benefit of separating prefill and decode."
                            ),
                            "benchmark": {
                                "matrix": [{"isl": 5000, "osl": 250}],
                                "concurrency_stages": [
                                    {"concurrency": 1, "num_requests": 32},
                                    {"concurrency": 16, "num_requests": 96},
                                    {"concurrency": 45, "num_requests": 180},
                                ],
                            },
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": "Run the reference long-input workload and aggregate control.",
                    "scenario_ids": ["long-input"],
                },
                {
                    "id": "capacity",
                    "label": "Capacity / SLO",
                    "description": "Focus on the long-input load sweep and stable serving boundary.",
                    "scenario_ids": ["long-input"],
                },
            ],
            "comparison_arms": ["aggregated", "selected-pd-topologies"],
            "evidence": ["P/D routing decisions", "KV handoff", "Prefill and Decode pressure", "TTFT/ITL trade-off"],
            "validation": [
                "Prefill role discovery",
                "Decode role discovery",
                "KV transfer path",
                "Phase-separated metrics",
            ],
            "recommended_workload": {
                "kind": "matrix",
                "matrix": [
                    {"isl": 1024, "osl": 128},
                    {"isl": 8192, "osl": 128},
                    {"isl": 16384, "osl": 128},
                ],
                "concurrency_stages": [
                    {"concurrency": 1, "num_requests": 32},
                    {"concurrency": 8, "num_requests": 32},
                    {"concurrency": 32, "num_requests": 96},
                    {"concurrency": 64, "num_requests": 192},
                ],
            },
        },
    },
    "tiered-prefix-cache": {
        "label": "Tiered prefix cache",
        "variants": ["base", "native/cpu/base", "lmcache-connector/cpu/base"],
        "variant_labels": {
            "base": "HBM-only",
            "native/cpu/base": "Native CPU offload",
            "lmcache-connector/cpu/base": "LMCache CPU offload",
        },
        "variant_description": (
            "Each selected deployment overlay becomes an independent configuration and is "
            "evaluated with the same workload."
        ),
        "model_servers": ["vllm"],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": False,
        "evaluation": {
            "default_variant": "native/cpu/base",
            "required_baselines": ["direct-vllm"],
            "experiment_variable": "cache-tier",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full tiered-cache evaluation",
                    "recommended": True,
                    "description": "Compare HBM-only and tiered candidates under cache pressure.",
                }
            ],
            "comparison_arms": ["hbm-only", "selected-cache-tier"],
            "evidence": ["Working-set pressure", "Cache hit/offload", "Queue growth", "TTFT and capacity"],
            "validation": ["HBM cache", "Offload activity", "Metrics availability", "Baseline parity"],
            "recommended_workload": {
                "kind": "shared-prefix",
                "shared_prefix": {
                    "num_groups": 15,
                    "num_prompts_per_group": 5,
                    "system_prompt_len": 4000,
                    "question_len": 256,
                    "output_len": 256,
                    "enable_multi_turn_chat": False,
                    "interval": 60,
                    "stages": [
                        {"rate": 1.0, "duration": 60},
                        {"rate": 1.5, "duration": 60},
                        {"rate": 2.0, "duration": 60},
                        {"rate": 2.5, "duration": 60},
                        {"rate": 3.0, "duration": 60},
                    ],
                },
            },
            "unavailable_variants": [
                {
                    "id": "native/fs/base",
                    "label": "Native CPU + filesystem",
                    "reason": "The selected llm-d source does not contain this XPU overlay.",
                },
                {
                    "id": "lmcache-connector/fs/base",
                    "label": "LMCache CPU + filesystem",
                    "reason": "Requires RWX PVC provisioning and validation, which Prism does not orchestrate yet.",
                },
            ],
        },
    },
    "precise-prefix-cache-routing": {
        "label": "Precise prefix-cache routing",
        "variants": [],
        "model_servers": ["vllm"],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": True,
        "evaluation": {
            "default_variant": "",
            "required_baselines": ["kubernetes-service", "optimized-baseline"],
            "experiment_variable": "routing-policy",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full precise-routing evaluation",
                    "recommended": True,
                    "description": ("RR, approximate, and precise routing under reuse, load, and a low-reuse control."),
                    "scenarios": [
                        {
                            "id": "shared-prefix",
                            "name": "Precise prefix-routing performance",
                            "description": (
                                "Increase request rate for repeated long prompts and compare precise KV-aware routing "
                                "with Kubernetes round-robin."
                            ),
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload()},
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": "Compare Kubernetes RR with precise routing on the guide workload.",
                    "scenario_ids": ["shared-prefix"],
                },
                {
                    "id": "precise-value",
                    "label": "Precise value isolation",
                    "description": "Compare approximate and precise prefix knowledge with the same serving shape.",
                    "scenario_ids": ["shared-prefix"],
                },
            ],
            "comparison_arms": ["kubernetes-service", "optimized-baseline", "precise-prefix-cache-routing"],
            "evidence": [
                "KV-event/index health",
                "Cached prompt fraction",
                "Recomputed prompt tokens",
                "Warm-route locality",
            ],
            "validation": [
                "Model/tokenizer identity",
                "Block-size alignment",
                "KV publishers/subscribers",
                "Index lookup activity",
            ],
            "recommended_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload()},
        },
    },
}


def _supported_accelerators() -> list[str]:
    """Upstream guide variants the deploy providers can render.

    Derived from the registered hardware profiles (``upstream_variant``: xpu,
    gpu, ...) so a new vendor does not need this list edited; an Intel-only
    fallback keeps the history when hardware discovery is unavailable.
    """
    try:
        from llm_d_bench.hardware.registry import all_profiles

        variants = {profile.upstream_variant for profile in all_profiles() if profile.upstream_variant}
    except Exception:  # pragma: no cover - capabilities must not fail on discovery errors
        variants = set()
    return sorted(variants or {"xpu"})


#: PD-disaggregation variants per accelerator (the accelerator comes from the
#: selected cluster's hardware, never the Lens host's own profile). Variant ids
#: follow the guide tree under ``modelserver/<accelerator>/``:
#: - NVIDIA GPU: vLLM infra-provider overlays (``gpu/vllm/<INFRA_PROVIDER>``).
#:   Only the generic ``base`` overlay is supported today.
#: - Intel XPU: the two model-server overlays ``xpu/vllm`` and ``xpu/vllm-rdma``.
_PD_VARIANTS_BY_ACCELERATOR = {
    "xpu": ["vllm", "vllm-rdma"],
    "gpu": ["base"],
}
_PD_UNAVAILABLE_VARIANTS_BY_ACCELERATOR = {
    "gpu": [
        {"id": "coreweave", "label": "CoreWeave", "reason": "Cloud-provider overlay is not supported yet."},
        {"id": "gke/base", "label": "GKE", "reason": "Cloud-provider overlay is not supported yet."},
        {"id": "gke/a4x", "label": "GKE A4X", "reason": "Cloud-provider overlay is not supported yet."},
        {"id": "gke/a4xmax", "label": "GKE A4X Max", "reason": "Cloud-provider overlay is not supported yet."},
        {"id": "aws", "label": "AWS EFA", "reason": "Cloud-provider overlay is not supported yet."},
        {
            "id": "cks-mooncake",
            "label": "CKS / Mooncake",
            "reason": "Requires an InfiniBand/RDMA cluster that Prism does not orchestrate yet.",
        },
    ],
}


def _hardware_variants(provider: str, metadata: dict) -> dict:
    """Expose a provider's per-accelerator variants to the UI.

    The PD disaggregation variant set depends on the **selected cluster's**
    accelerator (``gpu`` for NVIDIA, ``xpu`` for Intel), not on the Lens host, so
    the capabilities carry a per-accelerator map and the UI picks by the cluster
    hardware. Variants declared unavailable are surfaced but not selectable.
    """
    if provider != "pd-disaggregation":
        return metadata
    variants = sorted({variant for items in _PD_VARIANTS_BY_ACCELERATOR.values() for variant in items})
    return {
        **metadata,
        "variants": variants,
        "variants_by_accelerator": _PD_VARIANTS_BY_ACCELERATOR,
        "unavailable_variants_by_accelerator": _PD_UNAVAILABLE_VARIANTS_BY_ACCELERATOR,
    }


def deployment_capabilities() -> list[dict]:
    return [
        {
            "id": provider,
            "supported": True,
            "accelerators": _supported_accelerators(),
            **_hardware_variants(provider, metadata),
        }
        for provider, metadata in _PROVIDER_CAPABILITIES.items()
    ]


def provider_capability(provider: str) -> dict | None:
    capability = _PROVIDER_CAPABILITIES.get(provider)
    return (
        None
        if capability is None
        else {
            "id": provider,
            "supported": True,
            "accelerators": _supported_accelerators(),
            **_hardware_variants(provider, capability),
        }
    )


def provider_supports(provider: str, capability: str) -> bool:
    """Return a boolean capability without exposing the mutable registry entry."""
    return bool(_PROVIDER_CAPABILITIES.get(provider, {}).get(capability, False))
