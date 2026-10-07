"""Resolve and freeze planning evidence owned by existing Prism domains."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from llm_d_bench.aic.models import AICRequest
from llm_d_bench.aic.service import AICError, check_support, search
from llm_d_bench.capacity import load_model_config
from llm_d_bench.cluster import registry
from llm_d_bench.cluster.service import build_overview
from llm_d_bench.deploy.application import deployment_run_manager
from llm_d_bench.simulation.service import list_tasks

from .benchmark_evidence import (
    BenchmarkMetrics,
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
)
from .benchmark_evidence import (
    WorkloadProfile as BenchmarkWorkloadProfile,
)
from .benchmark_store import JsonBenchmarkRecordStore
from .planner import AICCandidatePrediction, PlanningFacts, resolve_workload_signals

_GIB = 1024**3
logger = logging.getLogger(__name__)


class PlanningEvidence(BaseModel):
    id: str
    source: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ResolvedPlanningFacts(BaseModel):
    facts: PlanningFacts
    cluster_snapshot: dict[str, Any]
    evidence: list[PlanningEvidence] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)


async def resolve_planning_facts(
    cluster_server_id: str,
    model_name: str,
    requested: PlanningFacts,
    *,
    include_supplementary_evidence: bool = True,
) -> ResolvedPlanningFacts:
    """Use a live cluster overview for hard constraints and record other evidence."""
    requested = resolve_workload_signals(requested)
    cluster = registry.require_cluster(cluster_server_id)
    overview = await build_overview(cluster)
    hardware = overview.get("hardware") or {}
    available_gpus = int(hardware.get("availableGpuCount") or 0)
    gpu_count = int(hardware.get("gpuCount") or 0)
    vram_bytes = int(hardware.get("vramBytes") or 0)
    if gpu_count < 1 or vram_bytes < 1:
        raise ValueError("cluster overview does not report usable accelerator VRAM")
    selected_gpu_count = _select_gpu_capacity(requested, available_gpus, gpu_count)
    vram_per_gpu_gib = vram_bytes / gpu_count / _GIB
    memory_bytes = int(hardware.get("memoryBytes") or 0)
    memory_usage = hardware.get("memoryUsagePercent")
    cpu_buffer_gib = requested.cpu_buffer_gib
    if memory_bytes and isinstance(memory_usage, (int, float)):
        cpu_buffer_gib = max(0.0, memory_bytes * (1 - memory_usage / 100) / _GIB)

    cluster_evidence = PlanningEvidence(
        id=f"cluster-overview:{cluster.id}",
        source="cluster-overview",
        status="available",
        details={
            "cluster_id": cluster.id,
            "available_gpu_count": available_gpus,
            "total_gpu_count": gpu_count,
            "selected_gpu_count": selected_gpu_count,
            "capacity_mode": requested.hardware_capacity_mode,
            "vram_per_gpu_gib": vram_per_gpu_gib,
            "cpu_buffer_gib": cpu_buffer_gib,
        },
    )
    evidence = [cluster_evidence]
    if include_supplementary_evidence:
        evidence.extend(await _supplementary_evidence(model_name, requested, selected_gpu_count))

    model_config_dict = requested.model_config_dict
    if model_config_dict is None and model_name:
        loaded = load_model_config(model_name)
        if loaded is not None:
            model_config_dict = loaded.raw if hasattr(loaded, "raw") else getattr(loaded, "to_dict", lambda: None)()
            if model_config_dict is not None and isinstance(model_config_dict, dict) and include_supplementary_evidence:
                evidence.append(PlanningEvidence(
                    id=f"capacity-planner:{model_name}",
                    source="capacity-planner",
                    status="available",
                    details={
                        "model_name": model_name,
                        "architectures": model_config_dict.get("architectures", []),
                        "num_attention_heads": model_config_dict.get("num_attention_heads"),
                    },
                ))

    aic_predictions = tuple(
        prediction
        for item in evidence
        if item.id == "aic:search" and item.status == "available"
        for prediction in _aic_predictions(item.details.get("configs", []))
    )
    facts = resolve_workload_signals(
        PlanningFacts(
            model_weight_gib=requested.model_weight_gib,
            vram_per_gpu_gib=vram_per_gpu_gib,
            free_gpu_count=selected_gpu_count,
            cpu_buffer_gib=cpu_buffer_gib,
            required_cpu_buffer_gib=requested.required_cpu_buffer_gib,
            context_length=requested.context_length,
            shared_prefix_ratio=requested.shared_prefix_ratio,
            prefill_heavy=requested.prefill_heavy,
            use_case=requested.use_case,
            ttft_slo_ms=requested.ttft_slo_ms,
            tpot_slo_ms=requested.tpot_slo_ms,
            end_to_end_latency_slo_ms=requested.end_to_end_latency_slo_ms,
            operator_preference=requested.operator_preference,
            vllm_arguments=requested.vllm_arguments,
            evidence_ids=tuple(item.id for item in evidence),
            workload_profile=requested.workload_profile,
            aic_predictions=aic_predictions,
            hardware_capacity_mode=requested.hardware_capacity_mode,
            custom_gpu_count=requested.custom_gpu_count,
            model_name=model_name or requested.model_name,
            model_config_dict=model_config_dict,
        )
    )
    resolved = ResolvedPlanningFacts(
        facts=facts,
        cluster_snapshot=overview,
        evidence=evidence,
        trace=[
            {
                "phase": "workload",
                "status": "complete",
                "profile": asdict(facts.workload_profile) if facts.workload_profile else None,
                "ttft_slo_ms": facts.ttft_slo_ms,
                "tpot_slo_ms": facts.tpot_slo_ms,
                "context_length": facts.context_length,
            },
            {
                "phase": "facts",
                "status": "complete",
                "available_gpu_count": available_gpus,
                "selected_gpu_count": selected_gpu_count,
                "vram_per_gpu_gib": vram_per_gpu_gib,
                "cpu_buffer_gib": cpu_buffer_gib,
                "evidence": [{"id": item.id, "status": item.status} for item in evidence],
                "aic_prediction_count": len(aic_predictions),
            },
            {
                "phase": "aic",
                "status": "requested" if include_supplementary_evidence else "skipped",
                "request": {
                    "model_name": model_name,
                    "gpu_count": max(selected_gpu_count, 1),
                    "mean_input_tokens": int(facts.workload_profile.mean_input_tokens)
                    if facts.workload_profile and facts.workload_profile.mean_input_tokens
                    else facts.context_length,
                    "mean_output_tokens": int(facts.workload_profile.mean_output_tokens)
                    if facts.workload_profile and facts.workload_profile.mean_output_tokens
                    else 256,
                    "ttft_target_ms": facts.ttft_slo_ms,
                    "tpot_target_ms": facts.tpot_slo_ms,
                }
                if include_supplementary_evidence
                else None,
                "support_status": next((item.status for item in evidence if item.id == "aic:support"), "not_requested"),
                "search_status": next((item.status for item in evidence if item.id == "aic:search"), "not_requested"),
                "prediction_count": len(aic_predictions),
            },
        ],
    )
    logger.info(
        "Agentic facts cluster=%s selected_gpus=%d aic_predictions=%d evidence=%s",
        cluster_server_id,
        selected_gpu_count,
        len(aic_predictions),
        [(item.id, item.status) for item in evidence],
    )
    return resolved


def _select_gpu_capacity(requested: PlanningFacts, available_gpus: int, gpu_count: int) -> int:
    if requested.hardware_capacity_mode == "free":
        return available_gpus
    if requested.hardware_capacity_mode == "all":
        return gpu_count
    if requested.custom_gpu_count is None or requested.custom_gpu_count < 1:
        raise ValueError("custom GPU capacity requires a positive custom_gpu_count")
    if requested.custom_gpu_count > gpu_count:
        raise ValueError("custom GPU capacity cannot exceed total cluster GPUs")
    return requested.custom_gpu_count


async def _supplementary_evidence(
    model_name: str,
    facts: PlanningFacts,
    gpu_count: int,
) -> list[PlanningEvidence]:
    evidence: list[PlanningEvidence] = []
    try:
        request = AICRequest(
            model_name=model_name,
            gpu_count=max(gpu_count, 1),
            mean_input_tokens=int(facts.workload_profile.mean_input_tokens)
            if facts.workload_profile and facts.workload_profile.mean_input_tokens
            else facts.context_length,
            mean_output_tokens=int(facts.workload_profile.mean_output_tokens)
            if facts.workload_profile and facts.workload_profile.mean_output_tokens
            else 256,
            ttft_target_ms=facts.ttft_slo_ms,
            tpot_target_ms=facts.tpot_slo_ms,
            max_candidates=3,
        )
        support = await check_support(request)
        evidence.append(
            PlanningEvidence(
                id="aic:support",
                source="aic",
                status="available",
                details=support.model_dump(),
            )
        )
        if support.supported:
            prediction = await search(request)
            evidence.append(
                PlanningEvidence(
                    id="aic:search",
                    source="aic",
                    status="available",
                    details=prediction.model_dump(),
                )
            )
    except (AICError, ValueError):
        evidence.append(PlanningEvidence(id="aic:support", source="aic", status="unavailable"))
    try:
        history_count = len(deployment_run_manager.store.list_runs())
        evidence.append(
            PlanningEvidence(
                id="deploy-history",
                source="deploy-history",
                status="available",
                details={"run_count": history_count},
            )
        )
    except OSError:
        evidence.append(PlanningEvidence(id="deploy-history", source="deploy-history", status="unavailable"))
    try:
        simulations = await list_tasks(status="completed", model=model_name, limit=20)
        topology_by_execution = _historical_topology_by_execution()
        observations = await _similar_historical_observations(simulations.tasks, facts, topology_by_execution)
        _persist_benchmark_records(model_name, facts, observations)
        evidence.append(
            PlanningEvidence(
                id="simulation-history",
                source="simulation",
                status="available",
                details={
                    "total": simulations.total,
                    "benchmark_record_count": len(JsonBenchmarkRecordStore().list()),
                },
            )
        )
    except (OSError, RuntimeError, ValueError):
        evidence.append(PlanningEvidence(id="simulation-history", source="simulation", status="unavailable"))
    return evidence


def _aic_predictions(configs: list[dict[str, Any]]) -> list[AICCandidatePrediction]:
    """Normalize only AIC configurations that can match Prism's bounded topologies."""
    predictions: list[AICCandidatePrediction] = []
    for config in configs:
        try:
            mode = config.get("mode")
            if mode == "agg":
                predictions.append(
                    AICCandidatePrediction(
                        mode="agg",
                        tensor_parallel_size=int(config["tp"]),
                        replicas=int(config["replicas"]),
                        ttft_ms=_positive_float(config.get("ttft_ms")),
                        tpot_ms=_positive_float(config.get("tpot_ms")),
                        throughput_tokens_per_sec=_positive_float(config.get("throughput_tokens_per_sec")),
                    )
                )
            elif mode == "disagg":
                predictions.append(
                    AICCandidatePrediction(
                        mode="disagg",
                        tensor_parallel_size=int(config["decode_tp"]),
                        replicas=int(config["decode_replicas"]),
                        prefill_tensor_parallel_size=int(config["prefill_tp"]),
                        prefill_replicas=int(config["prefill_replicas"]),
                        ttft_ms=_positive_float(config.get("ttft_ms")),
                        tpot_ms=_positive_float(config.get("tpot_ms")),
                        throughput_tokens_per_sec=_positive_float(config.get("throughput_tokens_per_sec")),
                    )
                )
        except (KeyError, TypeError, ValueError):
            continue
    return predictions


def _positive_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _historical_topology_by_execution() -> dict[str, dict[str, Any]]:
    topologies: dict[str, dict[str, Any]] = {}
    for run in deployment_run_manager.store.list_runs():
        for case in run.cases:
            if not case.execution_id:
                continue
            configuration = run.source_configurations[case.source_configuration_ordinal]
            topology = _configuration_topology(configuration)
            if topology is not None:
                topologies[case.execution_id] = topology
    return topologies


def _configuration_topology(configuration) -> dict[str, Any] | None:
    content = configuration.content
    decode = content.get("decode") if isinstance(content, dict) else None
    if not isinstance(decode, dict):
        return None
    replicas = _positive_int(decode.get("replicaCount"))
    tensor_parallel_size = _positive_int(decode.get("tensorParallelSize"))
    if replicas is None or tensor_parallel_size is None:
        return None
    prefill = content.get("prefill") if isinstance(content, dict) else None
    prefill_replicas = _positive_int(prefill.get("replicaCount")) if isinstance(prefill, dict) else None
    prefill_tensor_parallel_size = (
        _positive_int(prefill.get("tensorParallelSize")) if isinstance(prefill, dict) else None
    )
    required_gpus = replicas * tensor_parallel_size
    if prefill_replicas and prefill_tensor_parallel_size:
        required_gpus += prefill_replicas * prefill_tensor_parallel_size
    return {
        "candidate_id": configuration.provenance.get("agentic_candidate_id"),
        "provider_ref": configuration.provider_ref,
        "replicas": replicas,
        "tensor_parallel_size": tensor_parallel_size,
        "prefill_replicas": prefill_replicas,
        "prefill_tensor_parallel_size": prefill_tensor_parallel_size,
        "guide_variant": content.get("guideVariant"),
        "max_model_len": _positive_int(decode.get("maxModelLen")),
        "gpu_memory_utilization": _positive_float(decode.get("gpuMemoryUtilization")),
        "required_gpus": required_gpus,
    }


async def _similar_historical_observations(
    tasks,
    facts: PlanningFacts,
    topology_by_execution: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for task in tasks:
        result = task.result
        if result is None:
            continue
        requests = [request for request in result.per_request if request.successful is not False]
        ttft_values = sorted(request.ttft_ms for request in requests if request.ttft_ms and request.ttft_ms > 0)
        tpot_values = sorted(request.tpot_ms for request in requests if request.tpot_ms and request.tpot_ms > 0)
        throughput = _positive_float(result.summary.get("throughput_tps"))
        if not ttft_values and not tpot_values and throughput is None:
            continue
        observed_input = _mean(request.input_tokens for request in requests)
        observed_output = _mean(request.output_tokens for request in requests)
        similarity = _workload_similarity(facts, task.scenario, observed_input, observed_output)
        topology = topology_by_execution.get(getattr(task, "endpoint_deployment_execution_id", None) or "", {})
        observations.append(
            {
                **topology,
                "task_id": task.id,
                "scenario": task.scenario,
                "similarity": similarity,
                "ttft_p95_ms": _p95(ttft_values),
                "tpot_p95_ms": _p95(tpot_values),
                "throughput_tokens_per_sec": throughput,
            }
        )
    return sorted(observations, key=lambda item: (-item["similarity"], item["task_id"]))[:3]


def _persist_benchmark_records(model_name: str, facts: PlanningFacts, observations: list[dict[str, Any]]) -> None:
    """Normalize completed Evaluate observations into versioned, reusable benchmark evidence."""
    profile = facts.workload_profile
    if profile is None:
        return
    store = JsonBenchmarkRecordStore()
    for observation in observations:
        if not observation.get("tensor_parallel_size") or not observation.get("replicas"):
            continue
        store.save(
            BenchmarkRecord(
                benchmark_id=f"evaluate:{observation['task_id']}",
                timestamp=datetime.now(UTC),
                outcome="succeeded",
                model=ModelDescriptor(
                    model_id=model_name,
                    family=model_name.split("/")[0],
                    architecture="dense",
                    parameter_count_b=max(facts.model_weight_gib / 2, 0.1),
                    quantization="unknown",
                    tokenizer_id=model_name,
                ),
                hardware=HardwareDescriptor(
                    accelerator="cluster",
                    vram_per_gpu_gib=facts.vram_per_gpu_gib,
                    gpu_count=max(observation.get("required_gpus") or 1, 1),
                ),
                runtime=RuntimeFingerprint(backend="vllm", backend_version="unknown", accelerator_runtime="unknown"),
                workload=BenchmarkWorkloadProfile(
                    mean_input_tokens=profile.mean_input_tokens or facts.context_length,
                    p95_input_tokens=profile.p95_input_tokens or profile.mean_input_tokens or facts.context_length,
                    mean_output_tokens=profile.mean_output_tokens or 256,
                    concurrency=profile.concurrency or 1,
                    request_rate=profile.request_rate or 1,
                    shared_prefix_ratio=profile.shared_prefix_ratio or 0,
                    prefill_fraction=1.0 if facts.prefill_heavy else 0.0,
                ),
                configuration=CandidateConfiguration(
                    topology=observation.get("provider_ref") or "baseline-vllm",
                    decode_tp=observation["tensor_parallel_size"],
                    decode_replicas=observation["replicas"],
                    optimizations=(observation.get("guide_variant") or "",),
                ),
                metrics=BenchmarkMetrics(
                    ttft_p95_ms=observation.get("ttft_p95_ms"),
                    tpot_p95_ms=observation.get("tpot_p95_ms"),
                    throughput_tokens_per_s=observation.get("throughput_tokens_per_sec"),
                ),
            )
        )


def _mean(values) -> float | None:
    positive = [float(value) for value in values if value and value > 0]
    return sum(positive) / len(positive) if positive else None


def _p95(values: list[float]) -> float | None:
    return values[min(len(values) - 1, int(len(values) * 0.95))] if values else None


def _workload_similarity(
    facts: PlanningFacts,
    scenario: str,
    observed_input: float | None,
    observed_output: float | None,
) -> float:
    profile = facts.workload_profile
    target_input = profile.mean_input_tokens if profile else None
    target_output = profile.mean_output_tokens if profile else None
    distances = [0.0 if scenario == _scenario_for_use_case(facts.use_case) else 0.25]
    for target, observed in ((target_input, observed_input), (target_output, observed_output)):
        if target and observed:
            distances.append(min(abs(target - observed) / max(target, observed), 1.0))
    return round(max(0.0, 1 - sum(distances) / len(distances)), 3)


def _scenario_for_use_case(use_case: str) -> str:
    return "coding" if use_case == "code-generation" else "chat"
