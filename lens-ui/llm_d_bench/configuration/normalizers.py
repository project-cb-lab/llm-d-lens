# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Normalize source-specific configuration into CandidateConfig."""

from typing import Any

import yaml

from .models import CandidateConfig, CandidateSource, ResolveTarget, SourceConfiguration

_SOURCE_TYPES = {
    "aic": None,
    "baseline_search": "baseline",
    "pd_search": "pd",
    "epd_search": "epd",
    "tiered_cache_search": "tiered_cache",
    "historical": None,
    "manual": None,
}
_TYPE_ALIASES = {
    "agg": "baseline",
    "aggregated": "baseline",
    "disagg": "pd",
    "tiered-cache": "tiered_cache",
    "tiered_cache_search": "tiered_cache",
}

_DEFAULT_NETWORK = {
    "protocol": "HTTP",
    "inference_pool": {
        "target_port_number": 8000,
        "selector": {"llm-d.ai/inferenceServing": "true"},
        "extension_ref": {
            "port_number": 9002,
            "failure_mode": "FailClose",
        },
    },
    "http_route": {
        "gateway_discovery": "first_in_namespace",
        "path_prefix": "/",
        "request_timeout": "0s",
        "backend_request_timeout": "0s",
    },
}


def parse_payload(configuration: SourceConfiguration) -> dict[str, Any]:
    """Parse object or YAML input and guarantee an object payload."""
    if isinstance(configuration.payload, dict):
        return configuration.payload
    try:
        payload = yaml.safe_load(configuration.payload)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Configuration YAML must contain a mapping at its root")
    return payload


def _positive_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Expected an integer, received {value!r}") from error
    return parsed


def _component(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    nested = payload.get(name)
    nested = nested if isinstance(nested, dict) else {}
    tp = nested.get("tensor_parallel_size", nested.get("tensor_parallel", nested.get("tp", payload.get(f"{name}_tp"))))
    replicas = nested.get("replicas", nested.get("workers", payload.get(f"{name}_replicas")))
    if tp is None and replicas is None:
        return None
    result: dict[str, Any] = {
        "tensor_parallel_size": _positive_int(tp, 1),
        "replicas": _positive_int(replicas, 1),
    }
    batch_size = nested.get("batch_size", payload.get(f"{name}_batch_size"))
    if batch_size is not None:
        result["batch_size"] = _positive_int(batch_size)
    return result


def _serving(payload: dict[str, Any]) -> dict[str, Any] | None:
    nested = payload.get("serving")
    nested = nested if isinstance(nested, dict) else {}
    tp = nested.get(
        "tensor_parallel_size",
        nested.get("tensor_parallel", nested.get("tp", payload.get("tensor_parallel", payload.get("tp")))),
    )
    replicas = nested.get("replicas", payload.get("replicas"))
    max_model_len = nested.get("max_model_len", payload.get("max_model_len"))
    if tp is None and replicas is None and max_model_len is None:
        return None
    result: dict[str, Any] = {}
    if tp is not None or replicas is not None:
        result["tensor_parallel_size"] = _positive_int(tp, 1)
        result["replicas"] = _positive_int(replicas, 1)
    if max_model_len is not None:
        result["max_model_len"] = _positive_int(max_model_len)
    return result


def _deployment_type(source: CandidateSource, payload: dict[str, Any]) -> str:
    raw_type = payload.get("type", payload.get("topology_mode", payload.get("mode")))
    if raw_type:
        normalized = _TYPE_ALIASES.get(str(raw_type).lower(), str(raw_type).lower())
    else:
        normalized = _SOURCE_TYPES[source.name]
    if normalized not in {"baseline", "pd", "epd", "tiered_cache"}:
        raise ValueError("Configuration type must be baseline, pd, epd, or tiered_cache")
    return normalized


def normalize_configuration(
    source: CandidateSource,
    configuration: SourceConfiguration,
    target: ResolveTarget,
) -> CandidateConfig:
    """Build a source-independent CandidateConfig."""
    payload = parse_payload(configuration)
    deployment_type = _deployment_type(source, payload)
    model = payload.get("model") or payload.get("model_name")
    hardware = payload.get("hardware") if isinstance(payload.get("hardware"), dict) else {}
    target_payload = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    target_config: dict[str, Any] = {**target_payload}
    if target.cluster_id:
        target_config["cluster_id"] = target.cluster_id
    if model:
        target_config["model"] = model
    if hardware:
        target_config["hardware"] = hardware

    source_with_result = source.model_copy(
        update={"result_id": configuration.result_id or source.result_id or configuration.configuration_id}
    )
    serving = _serving(payload)
    prefill = _component(payload, "prefill")
    decode = _component(payload, "decode")
    encode = _component(payload, "encode")
    cache = payload.get("cache") if isinstance(payload.get("cache"), dict) else None
    resources = payload.get("resources") if isinstance(payload.get("resources"), dict) else None
    network = payload.get("network") if isinstance(payload.get("network"), dict) else _DEFAULT_NETWORK
    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else None
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else None

    if deployment_type in {"baseline", "tiered_cache"} and serving is None and decode is not None:
        serving, decode = decode, None

    return CandidateConfig(
        type=deployment_type,
        candidate_source=source_with_result,
        target=target_config,
        serving=serving,
        encode=encode,
        prefill=prefill,
        decode=decode,
        cache=cache,
        resources=resources,
        network=network,
        performance=performance,
        runtime=runtime,
    )
