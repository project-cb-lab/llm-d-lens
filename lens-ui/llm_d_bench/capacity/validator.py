"""Parameter validation and diagnostics for vLLM deployments against model and GPU constraints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .capacity_planner import (
    KVCacheDetail,
    allocatable_kv_cache_memory,
    available_gpu_memory,
    estimate_vllm_activation_memory,
    estimate_vllm_cuda_graph_memory,
    estimate_vllm_non_torch_memory,
    find_possible_tp,
    get_text_config,
    gpus_required,
    load_model_config,
    max_concurrent_requests,
    max_context_len,
    model_memory_req,
    model_total_params,
)

logger = logging.getLogger(__name__)


class _Logger(Protocol):
    """Minimal logger interface compatible with both standard and custom loggers."""

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


@dataclass
class ValidationParams:
    """Parameters for capacity validation of a deployment topology."""

    models: list[str]
    gpu_memory: float  # GPU memory per device in GiB (0 = unknown/skip GPU memory checks)
    tp: int
    pp: int = 1
    dp: int = 1
    accelerator_nr: int = 0  # Total accelerator cards assigned/available
    gpu_memory_util: float = 0.9
    max_model_len: int = 4096
    replicas: int = 1
    hf_token: str | None = None
    ignore_failures: bool = False
    label: str = ""  # e.g., "standalone", "prefill", "decode"
    model_config: Any | None = None
    fallback_weight_gib: float | None = None


class CapacityEvaluationResult(BaseModel):
    """Structured capacity calculation and diagnostic outcome."""

    is_deployable: bool
    allocatable_kv_cache_gib: float
    per_request_kv_cache_gib: float
    max_concurrent_requests: int
    memory_breakdown: dict[str, float] = Field(default_factory=dict)
    rejection_reasons: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    possible_tps: list[int] = Field(default_factory=list)


def _log_config_suggestions(msg_fn: Any, params: ValidationParams) -> None:
    """Generate actionable remediation suggestions for memory-constrained deployments."""
    next_tp = params.tp * 2
    msg_fn(f"Suggestion: Increase Tensor Parallelism (e.g. TP={next_tp}) to shard model across more GPUs.")
    if params.max_model_len > 2048:
        half_ctx = max(2048, params.max_model_len // 2)
        msg_fn(f"Suggestion: Decrease max_model_len from {params.max_model_len} to {half_ctx} to reduce KV cache requirements.")
    if params.gpu_memory_util < 0.95:
        msg_fn("Suggestion: If GPU is dedicated, consider increasing gpu_memory_utilization up to 0.95.")


def validate_vllm_params(
    params: ValidationParams,
    custom_logger: Any | None = None,
) -> list[str]:
    """Validate vLLM parameters against capacity planner and return diagnostic messages."""
    log = custom_logger or logger
    tag = "WARNING" if params.ignore_failures else "ERROR"
    prefix = f"[{params.label}] " if params.label else ""
    messages: list[str] = []

    def log_err(text: str) -> None:
        full = f"{prefix}{tag}: {text}"
        messages.append(full)
        log.warning(full)

    def log_info(text: str) -> None:
        full = f"{prefix}INFO: {text}"
        messages.append(full)
        log.info(full)

    per_replica_gpus = gpus_required(tp=params.tp, pp=params.pp, dp=params.dp)
    total_required_gpus = per_replica_gpus * params.replicas if params.replicas > 0 else 0

    if params.accelerator_nr > 0 and per_replica_gpus > params.accelerator_nr:
        log_err(
            f"Accelerator requested is {params.accelerator_nr} but "
            f"TP x PP x DP = {params.tp} x {params.pp} x {params.dp} "
            f"= {per_replica_gpus} GPUs are required per replica"
        )

    if params.accelerator_nr > 0 and 0 < per_replica_gpus < params.accelerator_nr:
        log_info(
            f"Each replica requires {per_replica_gpus} GPUs, but "
            f"{params.accelerator_nr} requested per pod. Some GPUs may be idle."
        )

    skip_gpu_tests = params.gpu_memory <= 0
    if skip_gpu_tests:
        log_info("GPU memory unknown or 0. Skipping GPU memory and KV cache checks.")

    for model in params.models:
        model_config = params.model_config or load_model_config(model, hf_token=params.hf_token)
        text_config = get_text_config(model_config) if model_config is not None else None

        if model_config is not None and text_config is not None:
            try:
                valid_tp = find_possible_tp(model_config)
                if params.tp not in valid_tp:
                    log_err(
                        f"TP={params.tp} is invalid for {model}. "
                        f"Valid values: {valid_tp}"
                    )
            except Exception as exc:
                log_info(f"Could not compute valid TP values for {model}: {exc}")

            try:
                valid_max_ctx = max_context_len(model_config)
                if valid_max_ctx and params.max_model_len > valid_max_ctx:
                    log_err(
                        f"maxModelLen={params.max_model_len} exceeds "
                        f"model limit of {valid_max_ctx} for {model}"
                    )
            except Exception as exc:
                log_info(f"Could not determine max context length for {model}: {exc}")

        if not skip_gpu_tests:
            avail_mem = available_gpu_memory(params.gpu_memory, params.gpu_memory_util)
            log_info(
                f"{params.gpu_memory} GiB per GPU x {params.gpu_memory_util} "
                f"(utilization) = {avail_mem:.1f} GiB available per card."
            )

            # Memory allocation checks
            model_mem = model_memory_req(
                model,
                model_config,
                hf_token=params.hf_token,
                fallback_weight_gib=params.fallback_weight_gib,
            )
            activation_mem = estimate_vllm_activation_memory(model_config, tp=params.tp)
            cuda_graph_mem = estimate_vllm_cuda_graph_memory()
            non_torch_mem = estimate_vllm_non_torch_memory(params.tp, params.pp)

            avail_kv = allocatable_kv_cache_memory(
                model,
                model_config,
                params.gpu_memory,
                params.gpu_memory_util,
                tp=params.tp,
                pp=params.pp,
                dp=params.dp,
                max_model_len=params.max_model_len,
                batch_size=1,
                hf_token=params.hf_token,
                fallback_weight_gib=params.fallback_weight_gib,
            )

            if model_config is not None:
                kv_details = KVCacheDetail(model, model_config, params.max_model_len, batch_size=1)
                per_req_kv = kv_details.per_request_kv_cache_gb
            else:
                # Estimate per-request KV cache roughly: context_len / 4096 * 1.0 GiB
                per_req_kv = max(0.2, (params.max_model_len / 4096.0) * 1.0)

            if avail_kv < 0:
                log_err("DEPLOYMENT WILL FAIL: Insufficient GPU memory to load model.")
                log_err(
                    f"Model requires {abs(avail_kv):.2f} GiB MORE memory than "
                    f"available after loading weights ({model_mem:.2f} GiB) "
                    f"and activation memory ({activation_mem:.2f} GiB)."
                )
                _log_config_suggestions(log_err, params)
            elif avail_kv < per_req_kv:
                log_err("DEPLOYMENT WILL FAIL: Model loads but cannot serve any requests.")
                log_err(
                    f"Available KV cache: {avail_kv:.2f} GiB, required per request "
                    f"(max_model_len={params.max_model_len}): {per_req_kv:.2f} GiB"
                )
                _log_config_suggestions(log_err, params)
            else:
                concurrency = max_concurrent_requests(
                    model,
                    model_config,
                    params.max_model_len,
                    params.gpu_memory,
                    params.gpu_memory_util,
                    batch_size=1,
                    tp=params.tp,
                    pp=params.pp,
                    dp=params.dp,
                    hf_token=params.hf_token,
                    fallback_weight_gib=params.fallback_weight_gib,
                )
                log_info(
                    f"Allocatable KV cache: {avail_kv:.2f} GiB. "
                    f"Per-request KV: {per_req_kv:.2f} GiB. "
                    f"Max concurrent requests: {concurrency}."
                )

    return messages


def evaluate_capacity(params: ValidationParams) -> CapacityEvaluationResult:
    """Evaluate capacity and return structured result for agentic planning and MCP tools."""
    model = params.models[0] if params.models else "unknown"
    model_config = params.model_config or load_model_config(model, hf_token=params.hf_token)

    rejection_reasons: list[str] = []
    messages = validate_vllm_params(params)

    for msg in messages:
        if "DEPLOYMENT WILL FAIL:" in msg or "invalid for" in msg or "exceeds model limit" in msg:
            # Clean msg prefix
            clean = msg.split(": ", 1)[-1]
            rejection_reasons.append(clean)

    # Compute values
    model_mem = model_memory_req(
        model, model_config, hf_token=params.hf_token, fallback_weight_gib=params.fallback_weight_gib
    )
    act_mem = estimate_vllm_activation_memory(model_config, tp=params.tp)
    non_torch_mem = estimate_vllm_non_torch_memory(params.tp, params.pp)

    if params.gpu_memory > 0:
        allocatable_kv = allocatable_kv_cache_memory(
            model,
            model_config,
            params.gpu_memory,
            params.gpu_memory_util,
            tp=params.tp,
            pp=params.pp,
            dp=params.dp,
            max_model_len=params.max_model_len,
            batch_size=1,
            hf_token=params.hf_token,
            fallback_weight_gib=params.fallback_weight_gib,
        )
        if model_config is not None:
            kv_detail = KVCacheDetail(model, model_config, params.max_model_len, batch_size=1)
            per_req_kv = kv_detail.per_request_kv_cache_gb
        else:
            per_req_kv = max(0.2, (params.max_model_len / 4096.0) * 1.0)

        max_concurrent = max_concurrent_requests(
            model,
            model_config,
            params.max_model_len,
            params.gpu_memory,
            params.gpu_memory_util,
            batch_size=1,
            tp=params.tp,
            pp=params.pp,
            dp=params.dp,
            hf_token=params.hf_token,
            fallback_weight_gib=params.fallback_weight_gib,
        )
    else:
        allocatable_kv = 0.0
        per_req_kv = 0.0
        max_concurrent = 0

    deployable = len(rejection_reasons) == 0

    possible_tps: list[int] = []
    if model_config is not None:
        try:
            possible_tps = find_possible_tp(model_config)
        except Exception:
            possible_tps = []

    return CapacityEvaluationResult(
        is_deployable=deployable,
        allocatable_kv_cache_gib=round(allocatable_kv, 3),
        per_request_kv_cache_gib=round(per_req_kv, 3),
        max_concurrent_requests=max_concurrent,
        memory_breakdown={
            "model_weights_gib": round(model_mem, 2),
            "activation_memory_gib": round(act_mem, 2),
            "non_torch_overhead_gib": round(non_torch_mem, 2),
            "allocatable_kv_cache_gib": round(max(0.0, allocatable_kv), 2),
        },
        rejection_reasons=rejection_reasons,
        messages=messages,
        possible_tps=possible_tps,
    )
