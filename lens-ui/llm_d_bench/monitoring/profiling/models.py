"""DTOs for deployment profiling (Flow Map) API."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from llm_d_bench.monitoring.cluster_stack.models import StrictModel

ComponentRole = Literal["epp", "prefill", "decode"]


class FlowMapInstance(StrictModel):
    """A single serving instance (pod/service) with its own live metrics."""

    name: str
    request_rate: float | None = None
    arrival_request_rate: float | None = None
    success_request_rate: float | None = None
    failed_request_rate: float | None = None
    client_timeout_rate: float | None = None
    backend_error_rate: float | None = None
    input_token_rate: float | None = None
    output_token_rate: float | None = None
    queue_length: float | None = None
    # EPP only: how its in-flight requests split across the pipeline phases,
    # i.e. how many are sitting at the prefill servers versus the decode ones
    # (queued or executing). Together they account for queue_length.
    prefill_inflight: float | None = None
    decode_inflight: float | None = None
    kv_cache_usage_perc: float | None = None
    prefix_cache_hit_rate: float | None = None
    external_prefix_cache_hit_rate: float | None = None


class FlowMapComponent(StrictModel):
    """A flow-map node group: one component type (possibly several replicas)."""

    role: ComponentRole
    label: str
    present: bool
    instances: list[FlowMapInstance] = Field(default_factory=list)
    request_rate: float | None = None
    arrival_request_rate: float | None = None
    success_request_rate: float | None = None
    failed_request_rate: float | None = None
    client_timeout_rate: float | None = None
    backend_error_rate: float | None = None
    input_token_rate: float | None = None
    output_token_rate: float | None = None
    queue_length: float | None = None
    process_time_ms: float | None = None
    kv_cache_usage_perc: float | None = None
    prefix_cache_hit_rate: float | None = None
    external_prefix_cache_hit_rate: float | None = None


class FlowMapEdge(StrictModel):
    """A directed hop in the request pipeline, e.g. ``epp -> prefill``."""

    id: str
    source: str
    target: str
    request_rate: float | None = None
    success_request_rate: float | None = None
    failed_request_rate: float | None = None
    input_token_rate: float | None = None
    output_token_rate: float | None = None


class FlowMapResponse(StrictModel):
    execution_id: str
    namespace: str
    prometheus_reachable: bool
    message: str | None = None
    components: list[FlowMapComponent] = Field(default_factory=list)
    edges: list[FlowMapEdge] = Field(default_factory=list)
