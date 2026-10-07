"""Bounded, side-effect-free planning for Agentic Deploy."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from math import ceil
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from llm_d_bench.ai_providers.client import AIProviderClientError, client_for
from llm_d_bench.capacity import (
    KVCacheDetail,
    allocatable_kv_cache_memory,
    check_model_fits_gpu,
    find_possible_tp,
    load_model_config,
    max_concurrent_requests,
    wrap_config,
)

# Reasoning-capable providers (e.g. DeepSeek) spend part of the completion
# budget on hidden "reasoning_content" before writing the actual JSON answer;
# the default `complete()` budget (2048) is regularly exhausted by reasoning
# alone for a full candidate-scoring prompt, leaving zero tokens for the
# answer and surfacing as an empty-content failure. Give planning calls more
# headroom than typical single-turn completions need.
_PLANNER_MAX_TOKENS = 8192

# Reasoning-capable providers routinely take 30-50s to score a full candidate
# set (reasoning tokens are generated before the answer), well past a
# connectivity-check timeout like 30s. Planning requests need more slack than
# whatever short timeout a provider was saved with for Test Connection.
_PLANNER_MIN_TIMEOUT_SECONDS = 60.0
_BALANCED_MIN_THROUGHPUT_GAIN_PER_GPU = 2.0
logger = logging.getLogger(__name__)
PlannerProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_PROVIDER_GUIDANCE = (
    "- baseline-vllm: direct standard vLLM deployment without llm-d request routing.\n"
    "- optimized-baseline: vLLM with llm-d prefix-affinity and load-aware routing; useful for repeated prefixes, "
    "multiple replicas, or uneven request load.\n"
    "- pd-disaggregation: separate prefill and decode roles; useful for prefill-heavy or long-input workloads.\n"
    "- tiered-prefix-cache: vLLM with tiered prefix caching that uses available CPU buffer for eligible KV/cache data "
    "to relieve GPU VRAM pressure.\n"
    "- precise-prefix-cache-routing: vLLM deployment with precise prefix-cache routing."
)


def provider_guidance() -> str:
    """Return controlled, provider-specific semantics for AI planning prompts."""
    return _PROVIDER_GUIDANCE


class PlannedCandidate(BaseModel):
    """One bounded configuration consumable by a registered Deploy provider."""

    id: str
    provider_ref: Literal[
        "baseline-vllm",
        "optimized-baseline",
        "pd-disaggregation",
        "tiered-prefix-cache",
        "precise-prefix-cache-routing",
    ] = "baseline-vllm"
    replicas: int = Field(ge=1)
    tensor_parallel_size: int = Field(ge=1)
    prefill_replicas: int | None = Field(default=None, ge=1)
    prefill_tensor_parallel_size: int | None = Field(default=None, ge=1)
    guide_variant: str | None = None
    max_model_len: int = Field(ge=256)
    gpu_memory_utilization: float = Field(gt=0, le=1)
    required_gpus: int = Field(ge=1)
    deployable: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    historical_benchmarks: list[dict[str, object]] = Field(default_factory=list)
    performance_estimate: dict[str, float | None] | None = None
    performance_estimate_source: Literal["aic_estimate"] | None = None
    vllm_arguments: list[dict[str, str]] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0, le=1)
    score_source: Literal["deterministic", "openai-compatible"] | None = None
    slo_status: Literal["satisfied", "not_satisfied", "unknown"] = "unknown"
    slo_confidence: float = Field(default=0, ge=0, le=1)
    allocatable_kv_cache_gib: float | None = None
    per_request_kv_cache_gib: float | None = None
    max_concurrent_requests: int | None = None


class PlannerDecision(BaseModel):
    action: Literal["select_candidate", "cannot_satisfy"]
    candidate_id: str | None = None
    fallback_candidate_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    unmet_constraints: list[str] = Field(default_factory=list)
    slo_status: Literal["satisfied", "not_satisfied", "unknown"] = "unknown"
    slo_confidence: float = Field(default=0, ge=0, le=1)


class OpenAIPlannerSettings(BaseModel):
    """Connection details for an OpenAI-compatible, selection-only planner."""

    base_url: HttpUrl
    model: str = Field(min_length=1)
    api_key: str = ""
    provider_type: Literal["openai", "anthropic"] = "openai"
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class OpenAIPlannerRecommendation(BaseModel):
    """The sole response shape accepted from an external planning provider."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1, max_length=10)
    scores: dict[str, Annotated[float, Field(ge=0, le=1)]]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)


class OpenAIPlannerError(RuntimeError):
    """A configured external planner could not produce a valid bounded selection."""


def _parse_recommendation(content: str) -> OpenAIPlannerRecommendation:
    try:
        return OpenAIPlannerRecommendation.model_validate_json(content)
    except ValueError as original_error:
        decoder = json.JSONDecoder()
        valid: list[OpenAIPlannerRecommendation] = []
        for index, character in enumerate(content):
            if character != "{":
                continue
            try:
                value, _end = decoder.raw_decode(content, index)
                valid.append(OpenAIPlannerRecommendation.model_validate(value))
            except (json.JSONDecodeError, ValueError):
                continue
        if valid:
            return valid[-1]
        raise original_error


@dataclass(frozen=True)
class WorkloadProfile:
    """Measured traffic shape used to derive planning heuristics."""

    mean_input_tokens: float | None = None
    p95_input_tokens: float | None = None
    mean_output_tokens: float | None = None
    concurrency: float | None = None
    request_rate: float | None = None
    shared_prefix_ratio: float | None = None
    prefill_p95_ms: float | None = None
    decode_p95_ms: float | None = None


@dataclass(frozen=True)
class AICCandidatePrediction:
    """An AIC estimate that exactly corresponds to a bounded candidate topology."""

    mode: Literal["agg", "disagg"]
    tensor_parallel_size: int
    replicas: int
    prefill_tensor_parallel_size: int | None = None
    prefill_replicas: int | None = None
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    throughput_tokens_per_sec: float | None = None


@dataclass(frozen=True)
class WeightedBenchmarkSample:
    """One bounded historical benchmark with its actual result and relevance."""

    benchmark_id: str
    similarity: float
    time_decay: float
    runtime_similarity: float
    match_confidence: float
    observed_at: str
    outcome: Literal["succeeded", "failed"]
    ttft_p95_ms: float | None = None
    tpot_p95_ms: float | None = None
    throughput_tokens_per_s: float | None = None
    error_rate: float | None = None
    failure_reason: str | None = None
    topology: str = "baseline-vllm"
    decode_tp: int = 1
    decode_replicas: int = 1
    prefill_tp: int | None = None
    prefill_replicas: int | None = None
    optimizations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PerformanceScoreInput:
    """Candidate-level performance evidence that external planners cannot alter."""

    candidate_id: str
    source: Literal["benchmark"]
    status: Literal["measured", "interpolated", "estimated", "unknown"]
    ttft_p95_ms: float | None = None
    tpot_p95_ms: float | None = None
    throughput_tokens_per_s: float | None = None
    error_rate: float | None = None
    confidence: float = 0
    effective_sample_size: float = 0
    has_blocking_negative_evidence: bool = False
    evidence_refs: tuple[str, ...] = ()
    weighted_samples: tuple[WeightedBenchmarkSample, ...] = ()


@dataclass(frozen=True)
class PlanningFacts:
    model_weight_gib: float
    vram_per_gpu_gib: float
    free_gpu_count: int
    cpu_buffer_gib: float = 0
    required_cpu_buffer_gib: float = 0
    context_length: int = 4096
    shared_prefix_ratio: float = 0
    prefill_heavy: bool = False
    use_case: str = "general-chat"
    ttft_slo_ms: int | None = None
    tpot_slo_ms: int | None = None
    end_to_end_latency_slo_ms: int | None = None
    operator_preference: str = ""
    vllm_arguments: tuple[tuple[str, str], ...] = ()
    evidence_ids: tuple[str, ...] = ()
    workload_profile: WorkloadProfile | None = None
    aic_predictions: tuple[AICCandidatePrediction, ...] = ()
    performance_score_inputs: tuple[PerformanceScoreInput, ...] = ()
    hardware_capacity_mode: Literal["free", "all", "custom"] = "free"
    custom_gpu_count: int | None = None
    model_name: str | None = None
    model_config_dict: dict[str, Any] | None = None


def resolve_workload_signals(facts: PlanningFacts, *, prefill_heavy_threshold: float = 0.6) -> PlanningFacts:
    """Prefer measured workload signals while retaining old request fields as a compatibility fallback."""
    profile = facts.workload_profile
    use_case_defaults = {
        "code-generation": (0.6, False),
        "long-inputs": (0.0, True),
        "summarization": (0.0, True),
    }
    default_shared_prefix_ratio, default_prefill_heavy = use_case_defaults.get(facts.use_case, (0.0, False))
    if profile is None:
        return replace(
            facts,
            shared_prefix_ratio=facts.shared_prefix_ratio or default_shared_prefix_ratio,
            prefill_heavy=facts.prefill_heavy or default_prefill_heavy,
        )
    positive_fields = (
        profile.mean_input_tokens,
        profile.p95_input_tokens,
        profile.mean_output_tokens,
        profile.concurrency,
        profile.request_rate,
    )
    if any(value is not None and value <= 0 for value in positive_fields):
        raise ValueError("workload profile token, concurrency, and request-rate values must be positive")
    if profile.shared_prefix_ratio is not None and not 0 <= profile.shared_prefix_ratio <= 1:
        raise ValueError("workload profile shared prefix ratio must be between zero and one")
    if (profile.prefill_p95_ms is None) != (profile.decode_p95_ms is None):
        raise ValueError("workload profile prefill and decode P95 values must be provided together")
    has_measured_shape = profile.shared_prefix_ratio is not None or profile.prefill_p95_ms is not None
    has_legacy_shape = facts.shared_prefix_ratio > 0 or facts.prefill_heavy
    if profile.prefill_p95_ms is not None:
        if profile.prefill_p95_ms <= 0 or profile.decode_p95_ms is None or profile.decode_p95_ms <= 0:
            raise ValueError("workload profile prefill and decode P95 values must be positive")
        prefill_heavy = (
            profile.prefill_p95_ms / (profile.prefill_p95_ms + profile.decode_p95_ms) >= prefill_heavy_threshold
        )
    else:
        prefill_heavy = (
            default_prefill_heavy if not has_measured_shape and not has_legacy_shape else facts.prefill_heavy
        )
    return replace(
        facts,
        shared_prefix_ratio=(
            profile.shared_prefix_ratio
            if profile.shared_prefix_ratio is not None
            else (
                default_shared_prefix_ratio
                if not has_measured_shape and not has_legacy_shape
                else facts.shared_prefix_ratio
            )
        ),
        prefill_heavy=prefill_heavy,
    )


class DeterministicPlanner:
    """Generate and rank configurations for providers registered by Deploy."""

    tensor_parallelism_options = (1, 2, 4, 8, 16)
    replica_options = (1, 2, 4)

    def candidates(self, facts: PlanningFacts) -> list[PlannedCandidate]:
        facts = resolve_workload_signals(facts)
        if facts.model_weight_gib <= 0 or facts.vram_per_gpu_gib <= 0:
            raise ValueError("model weight and per-GPU VRAM must be positive")
        if facts.free_gpu_count < 0 or facts.cpu_buffer_gib < 0:
            raise ValueError("available resource quantities cannot be negative")
        if not 0 <= facts.shared_prefix_ratio <= 1:
            raise ValueError("shared prefix ratio must be between zero and one")

        # A conservative lower bound: 20% runtime headroom plus a context-sized KV floor.
        model_config = None
        if facts.model_config_dict is not None:
            model_config = wrap_config(facts.model_config_dict)
        elif facts.model_name:
            model_config = load_model_config(facts.model_name)

        valid_tps: list[int] | None = None
        if model_config is not None:
            try:
                valid_tps = find_possible_tp(model_config)
            except Exception:
                valid_tps = None

        footprint_gib = facts.model_weight_gib * 1.2 + max(1.0, facts.context_length / 4096)
        candidates: list[PlannedCandidate] = []
        for tensor_parallel_size in self.tensor_parallelism_options:
            for replicas in self.replica_options:
                required_gpus = tensor_parallel_size * replicas
                reasons: list[str] = []
                allocatable_kv_gib: float | None = None
                per_request_kv_gib: float | None = None
                concurrency: int | None = None

                if model_config is not None:
                    if valid_tps is not None and tensor_parallel_size not in valid_tps:
                        reasons.append(f"TP={tensor_parallel_size} is invalid for model architecture")
                else:
                    if footprint_gib > facts.vram_per_gpu_gib * tensor_parallel_size * 0.9:
                        reasons.append("estimated model footprint exceeds selected TP VRAM")

                avail_kv = allocatable_kv_cache_memory(
                    facts.model_name or "model",
                    model_config,
                    gpu_memory=facts.vram_per_gpu_gib,
                    gpu_util=0.9,
                    tp=tensor_parallel_size,
                    pp=1,
                    dp=1,
                    max_model_len=facts.context_length,
                    fallback_weight_gib=facts.model_weight_gib,
                )
                kv_detail = KVCacheDetail(
                    facts.model_name or "model",
                    model_config,
                    context_len=facts.context_length,
                    batch_size=1,
                )
                per_req_kv = kv_detail.per_request_kv_cache_gb
                concurrency = max_concurrent_requests(
                    facts.model_name or "model",
                    model_config,
                    max_model_len=facts.context_length,
                    gpu_memory=facts.vram_per_gpu_gib,
                    gpu_util=0.9,
                    tp=tensor_parallel_size,
                    pp=1,
                    dp=1,
                    fallback_weight_gib=facts.model_weight_gib,
                )
                allocatable_kv_gib = round(avail_kv, 3)
                per_request_kv_gib = round(per_req_kv, 3)

                if avail_kv < 0:
                    reasons.append("insufficient GPU memory to load model weights and activation")
                elif avail_kv < per_req_kv:
                    reasons.append("insufficient KV cache for context length")

                if required_gpus > facts.free_gpu_count:
                    reasons.append("insufficient free accelerator cards")
                candidates.extend(
                    self._single_role_candidates(
                        facts,
                        tensor_parallel_size,
                        replicas,
                        required_gpus,
                        reasons,
                        allocatable_kv_gib=allocatable_kv_gib,
                        per_request_kv_gib=per_request_kv_gib,
                        concurrency=concurrency,
                    )
                )
                candidates.append(
                    self._pd_candidate(
                        facts,
                        tensor_parallel_size,
                        replicas,
                        footprint_gib,
                        model_config=model_config,
                        valid_tps=valid_tps,
                    )
                )
        return [self._attach_performance_evidence(facts, candidate) for candidate in candidates]

    @staticmethod
    def _single_role_candidates(
        facts: PlanningFacts,
        tensor_parallel_size: int,
        replicas: int,
        required_gpus: int,
        reasons: list[str],
        allocatable_kv_gib: float | None = None,
        per_request_kv_gib: float | None = None,
        concurrency: int | None = None,
    ) -> list[PlannedCandidate]:
        shared_prefix_guides = [("precise-prefix-cache-routing", None)]
        if facts.cpu_buffer_gib >= facts.required_cpu_buffer_gib:
            shared_prefix_guides.append(("tiered-prefix-cache", "native/cpu/base"))
        providers = [("baseline-vllm", None), ("optimized-baseline", None), *shared_prefix_guides]
        return [
            PlannedCandidate(
                id=f"{provider}-tp{tensor_parallel_size}-r{replicas}",
                provider_ref=provider,
                replicas=replicas,
                tensor_parallel_size=tensor_parallel_size,
                guide_variant=variant,
                max_model_len=facts.context_length,
                gpu_memory_utilization=0.9,
                required_gpus=required_gpus,
                deployable=not reasons,
                rejection_reasons=list(reasons),
                evidence_ids=list(facts.evidence_ids),
                vllm_arguments=[{"name": name, "value": value} for name, value in facts.vllm_arguments],
                allocatable_kv_cache_gib=allocatable_kv_gib,
                per_request_kv_cache_gib=per_request_kv_gib,
                max_concurrent_requests=concurrency,
            )
            for provider, variant in providers
        ]

    @staticmethod
    def _pd_candidate(
        facts: PlanningFacts,
        tensor_parallel_size: int,
        replicas: int,
        footprint_gib: float,
        model_config: Any | None = None,
        valid_tps: list[int] | None = None,
    ) -> PlannedCandidate:
        required_gpus = tensor_parallel_size * replicas * 2
        reasons: list[str] = []
        allocatable_kv_gib: float | None = None
        per_request_kv_gib: float | None = None
        concurrency: int | None = None

        if model_config is not None:
            if valid_tps is not None and tensor_parallel_size not in valid_tps:
                reasons.append(f"TP={tensor_parallel_size} is invalid for model architecture")
        else:
            if footprint_gib > facts.vram_per_gpu_gib * tensor_parallel_size * 0.9:
                reasons.append("estimated model footprint exceeds selected TP VRAM")

        avail_kv = allocatable_kv_cache_memory(
            facts.model_name or "model",
            model_config,
            gpu_memory=facts.vram_per_gpu_gib,
            gpu_util=0.9,
            tp=tensor_parallel_size,
            pp=1,
            dp=1,
            max_model_len=facts.context_length,
            fallback_weight_gib=facts.model_weight_gib,
        )
        kv_detail = KVCacheDetail(
            facts.model_name or "model",
            model_config,
            context_len=facts.context_length,
            batch_size=1,
        )
        per_req_kv = kv_detail.per_request_kv_cache_gb
        concurrency = max_concurrent_requests(
            facts.model_name or "model",
            model_config,
            max_model_len=facts.context_length,
            gpu_memory=facts.vram_per_gpu_gib,
            gpu_util=0.9,
            tp=tensor_parallel_size,
            pp=1,
            dp=1,
            fallback_weight_gib=facts.model_weight_gib,
        )
        allocatable_kv_gib = round(avail_kv, 3)
        per_request_kv_gib = round(per_req_kv, 3)

        if avail_kv < 0:
            reasons.append("insufficient GPU memory to load model weights and activation")
        elif avail_kv < per_req_kv:
            reasons.append("insufficient KV cache for context length")

        if required_gpus > facts.free_gpu_count:
            reasons.append("insufficient free accelerator cards")
        return PlannedCandidate(
            id=f"pd-disaggregation-p{replicas}-d{replicas}-tp{tensor_parallel_size}",
            provider_ref="pd-disaggregation",
            replicas=replicas,
            tensor_parallel_size=tensor_parallel_size,
            prefill_replicas=replicas,
            prefill_tensor_parallel_size=tensor_parallel_size,
            guide_variant="vllm",
            max_model_len=facts.context_length,
            gpu_memory_utilization=0.9,
            required_gpus=required_gpus,
            deployable=not reasons,
            rejection_reasons=reasons,
            evidence_ids=list(facts.evidence_ids),
            vllm_arguments=[{"name": name, "value": value} for name, value in facts.vllm_arguments],
            allocatable_kv_cache_gib=allocatable_kv_gib,
            per_request_kv_cache_gib=per_request_kv_gib,
            max_concurrent_requests=concurrency,
        )

    def decide(self, facts: PlanningFacts, candidates: list[PlannedCandidate]) -> PlannerDecision:
        facts = resolve_workload_signals(facts)
        deployable = [candidate for candidate in candidates if candidate.deployable]
        if not deployable:
            reasons = sorted({reason for candidate in candidates for reason in candidate.rejection_reasons})
            return PlannerDecision(
                action="cannot_satisfy",
                confidence=1.0,
                rationale="No bounded Standard vLLM candidate satisfies the current resource constraints.",
                evidence_ids=list(facts.evidence_ids),
                unmet_constraints=reasons,
            )
        ranked = self._rank(facts, deployable)
        selected = ranked[0]
        selected_prediction = self._aic_prediction(facts, selected)
        slo_status, slo_confidence = self._slo_assessment(facts, selected)
        if selected_prediction is None:
            rationale = "Selected the smallest topology that passes VRAM, free-card, and CPU-buffer checks."
        elif self._has_latency_slo(facts):
            rationale = (
                "Selected an AIC-predicted topology that satisfies the requested latency targets."
                if slo_status == "satisfied"
                else "Selected the closest estimated topology, but the requested latency targets remain unmet."
            )
        else:
            rationale = "Selected an AIC-predicted topology because no latency targets were provided."
        return PlannerDecision(
            action="select_candidate",
            candidate_id=selected.id,
            fallback_candidate_ids=[candidate.id for candidate in ranked[1:3]],
            confidence=0.8 if selected_prediction else (0.7 if facts.evidence_ids else 0.5),
            rationale=rationale,
            evidence_ids=selected.evidence_ids,
            slo_status=slo_status,
            slo_confidence=slo_confidence,
        )

    def score(self, facts: PlanningFacts, candidates: list[PlannedCandidate]) -> list[PlannedCandidate]:
        facts = resolve_workload_signals(facts)
        deployable = [candidate for candidate in candidates if candidate.deployable]
        ranked = self._rank(facts, deployable)
        total = max(len(ranked), 1)
        return [
            candidate.model_copy(
                update={
                    "score": round(1 - index / total, 3),
                    "score_source": "deterministic",
                    "slo_status": self._slo_assessment(facts, candidate)[0],
                    "slo_confidence": self._slo_assessment(facts, candidate)[1],
                }
            )
            for index, candidate in enumerate(ranked)
        ]

    def _slo_assessment(
        self,
        facts: PlanningFacts,
        candidate: PlannedCandidate,
    ) -> tuple[Literal["satisfied", "not_satisfied", "unknown"], float]:
        if not self._has_latency_slo(facts):
            return "unknown", 0
        score_input = self._performance_score_input(facts, candidate)
        if self._usable_performance_evidence(score_input):
            assert score_input is not None
            return (
                "satisfied" if self._performance_slo_passes(facts, score_input) else "not_satisfied",
                score_input.confidence,
            )
        prediction = self._aic_prediction(facts, candidate)
        if prediction is not None:
            return ("satisfied" if self._aic_slo_passes(facts, prediction) else "not_satisfied"), 0.8
        return "unknown", 0

    def _rank(self, facts: PlanningFacts, candidates: list[PlannedCandidate]) -> list[PlannedCandidate]:
        balanced_candidates = self._balanced_candidates(facts, candidates)
        balanced_candidate_ids = {candidate.id for candidate in balanced_candidates}
        resource_preference = _resource_preference(facts.operator_preference)
        return sorted(
            candidates,
            key=lambda candidate: self._ranking_key(
                facts,
                candidate,
                balanced_candidate_ids,
                resource_preference,
            ),
        )

    def ranking_trace(self, facts: PlanningFacts, candidates: list[PlannedCandidate]) -> list[dict[str, Any]]:
        facts = resolve_workload_signals(facts)
        deployable = [candidate for candidate in candidates if candidate.deployable]
        if not deployable:
            return []
        balanced_candidate_ids = {candidate.id for candidate in self._balanced_candidates(facts, deployable)}
        resource_preference = _resource_preference(facts.operator_preference)
        ranking = [
            {
                "candidate_id": candidate.id,
                "rank": index + 1,
                "priority": [
                    str(value) if value == float("inf") else value
                    for value in self._ranking_key(
                        facts,
                        candidate,
                        balanced_candidate_ids,
                        resource_preference,
                    )
                ],
                "resource_preference": resource_preference,
                "aic_prediction": asdict(prediction) if prediction else None,
                "slo_status": self._slo_assessment(facts, candidate)[0],
            }
            for index, candidate in enumerate(self._rank(facts, deployable))
            for prediction in [self._aic_prediction(facts, candidate)]
        ]
        logger.info("Agentic deterministic ranking candidates=%d top=%s", len(ranking), ranking[0]["candidate_id"])
        return ranking

    def _ranking_key(
        self,
        facts: PlanningFacts,
        candidate: PlannedCandidate,
        balanced_candidate_ids: set[str],
        resource_preference: Literal["balanced", "performance", "economic"],
    ) -> tuple:
        return (
            self._operator_preference_priority(facts, candidate),
            self._performance_priority(facts, candidate),
            self._unmet_slo_priority(facts, candidate),
            self._resource_priority(candidate, resource_preference),
            self._guide_priority(facts, candidate),
            0 if candidate.id in balanced_candidate_ids else 1,
            self._aic_priority(facts, candidate),
            self._tensor_parallel_priority(candidate, resource_preference),
            candidate.required_gpus,
            candidate.tensor_parallel_size,
            -candidate.replicas,
        )

    @staticmethod
    def _resource_priority(
        candidate: PlannedCandidate, preference: Literal["balanced", "performance", "economic"]
    ) -> int:
        if preference == "performance":
            return -candidate.required_gpus
        if preference == "economic":
            return candidate.required_gpus
        return 0

    @staticmethod
    def _tensor_parallel_priority(
        candidate: PlannedCandidate, preference: Literal["balanced", "performance", "economic"]
    ) -> int:
        return candidate.tensor_parallel_size if preference in {"balanced", "economic"} else 0

    @staticmethod
    def _operator_preference_priority(facts: PlanningFacts, candidate: PlannedCandidate) -> int:
        preferred_provider = _preferred_provider(facts.operator_preference)
        if preferred_provider is not None:
            return 0 if candidate.provider_ref == preferred_provider else 1
        if _preference_requests_prefix_cache(facts.operator_preference):
            return 0 if candidate.provider_ref in {"tiered-prefix-cache", "precise-prefix-cache-routing"} else 1
        return 0

    def _balanced_candidates(self, facts: PlanningFacts, candidates: list[PlannedCandidate]) -> list[PlannedCandidate]:
        """Keep the minimum-resource candidates unless additional GPUs have proportional predicted throughput."""
        if not candidates:
            return []
        minimum_gpus = min(candidate.required_gpus for candidate in candidates)
        minimum_resource = [candidate for candidate in candidates if candidate.required_gpus == minimum_gpus]
        baseline_throughput = (
            max(
                (prediction.throughput_tokens_per_sec or 0)
                for candidate in minimum_resource
                if (prediction := self._aic_prediction(facts, candidate)) is not None
            )
            if any(self._aic_prediction(facts, candidate) is not None for candidate in minimum_resource)
            else None
        )
        if not baseline_throughput:
            # Without comparable performance evidence, retain the established workload/provider heuristic.
            return candidates
        efficient = list(minimum_resource)
        efficient_ids = {candidate.id for candidate in efficient}
        for candidate in candidates:
            prediction = self._aic_prediction(facts, candidate)
            if prediction is None:
                continue
            if prediction.throughput_tokens_per_sec is None:
                continue
            additional_gpus = candidate.required_gpus - minimum_gpus
            if (
                additional_gpus <= 0
                or prediction.throughput_tokens_per_sec - baseline_throughput
                >= baseline_throughput * _BALANCED_MIN_THROUGHPUT_GAIN_PER_GPU * additional_gpus / minimum_gpus
            ) and candidate.id not in efficient_ids:
                efficient.append(candidate)
                efficient_ids.add(candidate.id)
        return efficient or minimum_resource

    def _unmet_slo_priority(self, facts: PlanningFacts, candidate: PlannedCandidate) -> tuple[int, float, float]:
        prediction = self._aic_prediction(facts, candidate)
        if prediction is None or not self._has_latency_slo(facts) or self._aic_slo_passes(facts, prediction):
            return (0, 0, 0)
        violations = [
            max(0.0, value / target - 1)
            for value, target in ((prediction.ttft_ms, facts.ttft_slo_ms), (prediction.tpot_ms, facts.tpot_slo_ms))
            if value is not None and target is not None
        ]
        return (1, max(violations), sum(violations))

    def _aic_priority(self, facts: PlanningFacts, candidate: PlannedCandidate) -> tuple[int, float, float, float]:
        prediction = self._aic_prediction(facts, candidate)
        if prediction is None:
            return (1, float("inf"), float("inf"), float("inf"))
        if self._has_latency_slo(facts) and not self._aic_slo_passes(facts, prediction):
            return (2, float("inf"), float("inf"), float("inf"))
        return (
            0,
            prediction.ttft_ms if prediction.ttft_ms is not None else float("inf"),
            prediction.tpot_ms if prediction.tpot_ms is not None else float("inf"),
            -(prediction.throughput_tokens_per_sec or 0),
        )

    def _performance_priority(
        self,
        facts: PlanningFacts,
        candidate: PlannedCandidate,
    ) -> tuple[int, int, float, float, float, float, float, float]:
        score_input = self._performance_score_input(facts, candidate)
        if not self._usable_performance_evidence(score_input):
            return (1, 1, float("inf"), float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
        assert score_input is not None
        return (
            0,
            0 if self._performance_slo_passes(facts, score_input) else 1,
            -score_input.confidence,
            -score_input.effective_sample_size,
            score_input.ttft_p95_ms or float("inf"),
            score_input.tpot_p95_ms or float("inf"),
            -(score_input.throughput_tokens_per_s or 0),
            score_input.error_rate if score_input.error_rate is not None else float("inf"),
        )

    @staticmethod
    def _performance_score_input(
        facts: PlanningFacts,
        candidate: PlannedCandidate,
    ) -> PerformanceScoreInput | None:
        return next((item for item in facts.performance_score_inputs if item.candidate_id == candidate.id), None)

    @staticmethod
    def _usable_performance_evidence(score_input: PerformanceScoreInput | None) -> bool:
        return bool(
            score_input
            and not score_input.has_blocking_negative_evidence
            and score_input.confidence >= 0.5
            and (
                score_input.status == "measured"
                or (score_input.status == "interpolated" and score_input.effective_sample_size >= 3)
            )
        )

    @staticmethod
    def _performance_slo_passes(facts: PlanningFacts, score_input: PerformanceScoreInput) -> bool:
        return (
            facts.ttft_slo_ms is None
            or (score_input.ttft_p95_ms is not None and score_input.ttft_p95_ms <= facts.ttft_slo_ms)
        ) and (
            facts.tpot_slo_ms is None
            or (score_input.tpot_p95_ms is not None and score_input.tpot_p95_ms <= facts.tpot_slo_ms)
        )

    def _attach_performance_evidence(self, facts: PlanningFacts, candidate: PlannedCandidate) -> PlannedCandidate:
        score_input = self._performance_score_input(facts, candidate)
        if score_input is None:
            return candidate
        evidence_ids = [*candidate.evidence_ids, *score_input.evidence_refs]
        historical_benchmarks = [asdict(sample) for sample in score_input.weighted_samples]
        if not score_input.has_blocking_negative_evidence:
            return candidate.model_copy(
                update={
                    "evidence_ids": evidence_ids,
                    "historical_benchmarks": historical_benchmarks,
                }
            )
        return candidate.model_copy(
            update={
                "deployable": False,
                "rejection_reasons": [*candidate.rejection_reasons, "blocking benchmark failure evidence"],
                "evidence_ids": evidence_ids,
                "historical_benchmarks": historical_benchmarks,
            }
        )

    @staticmethod
    def _has_latency_slo(facts: PlanningFacts) -> bool:
        return facts.ttft_slo_ms is not None or facts.tpot_slo_ms is not None

    @staticmethod
    def _aic_slo_passes(facts: PlanningFacts, prediction: AICCandidatePrediction) -> bool:
        return (
            facts.ttft_slo_ms is None or prediction.ttft_ms is None or prediction.ttft_ms <= facts.ttft_slo_ms
        ) and (facts.tpot_slo_ms is None or prediction.tpot_ms is None or prediction.tpot_ms <= facts.tpot_slo_ms)

    @staticmethod
    def _aic_prediction(facts: PlanningFacts, candidate: PlannedCandidate) -> AICCandidatePrediction | None:
        for prediction in facts.aic_predictions:
            if (
                prediction.mode == "agg"
                and candidate.provider_ref in {"baseline-vllm", "optimized-baseline"}
                and prediction.tensor_parallel_size == candidate.tensor_parallel_size
                and prediction.replicas == candidate.replicas
            ):
                return prediction
            if (
                prediction.mode == "disagg"
                and candidate.provider_ref == "pd-disaggregation"
                and prediction.tensor_parallel_size == candidate.tensor_parallel_size
                and prediction.replicas == candidate.replicas
                and prediction.prefill_tensor_parallel_size == candidate.prefill_tensor_parallel_size
                and prediction.prefill_replicas == candidate.prefill_replicas
            ):
                return prediction
        return None

    @staticmethod
    def _guide_priority(facts: PlanningFacts, candidate: PlannedCandidate) -> int:
        preferred_provider = _preferred_provider(facts.operator_preference)
        if preferred_provider is not None:
            return 0 if candidate.provider_ref == preferred_provider else 1
        if facts.shared_prefix_ratio >= 0.5 or _preference_requests_prefix_cache(facts.operator_preference):
            return {
                "tiered-prefix-cache": 0,
                "precise-prefix-cache-routing": 1,
                "optimized-baseline": 2,
                "baseline-vllm": 3,
                "pd-disaggregation": 4,
            }[candidate.provider_ref]
        if facts.prefill_heavy:
            return {
                "pd-disaggregation": 0,
                "optimized-baseline": 1,
                "baseline-vllm": 2,
                "precise-prefix-cache-routing": 3,
                "tiered-prefix-cache": 4,
            }[candidate.provider_ref]
        return {
            "optimized-baseline": 0,
            "baseline-vllm": 1,
            "pd-disaggregation": 2,
            "precise-prefix-cache-routing": 3,
            "tiered-prefix-cache": 4,
        }[candidate.provider_ref]


def _preferred_provider(preference: str) -> str | None:
    normalized = preference.lower()
    if "offloading" in normalized or "offload" in normalized:
        return "tiered-prefix-cache"
    if "distributed" in normalized or "disaggregation" in normalized:
        return "pd-disaggregation"
    if "precise-caching" in normalized or "precise caching" in normalized:
        return "precise-prefix-cache-routing"
    return None


def _preference_requests_prefix_cache(preference: str) -> bool:
    normalized = preference.lower()
    return any(
        keyword in normalized
        for keyword in (
            "code-generation",
            "code generation",
            "cache",
            "caching",
            "routing",
            "prefix",
            "kv cache",
        )
    )


def _resource_preference(preference: str) -> Literal["balanced", "performance", "economic"]:
    normalized = preference.lower()
    if any(
        keyword in normalized
        for keyword in (
            "more gpu",
            "more gpus",
            "more accelerator",
            "maximum performance",
            "max performance",
            "performance",
            "more cards",
        )
    ):
        return "performance"
    if any(
        keyword in normalized
        for keyword in (
            "less gpu",
            "fewer gpu",
            "fewer gpus",
            "less resource",
            "fewer resource",
            "resource",
            "economic",
            "cost",
            "save gpu",
            "savegpu",
            "lessgpu",
        )
    ):
        return "economic"
    return "balanced"


def minimum_tensor_parallelism(facts: PlanningFacts) -> int:
    """Return the minimum TP implied by capacity planning or conservative memory lower bound."""
    model_config = None
    if facts.model_config_dict is not None:
        model_config = wrap_config(facts.model_config_dict)
    elif facts.model_name:
        model_config = load_model_config(facts.model_name)

    if model_config is not None:
        try:
            valid_fits = check_model_fits_gpu(
                facts.model_name or "model",
                model_config,
                gpu_memory_gb=facts.vram_per_gpu_gib,
                gpu_util=0.9,
                fallback_weight_gib=facts.model_weight_gib,
            )
            if valid_fits:
                return valid_fits[0]
        except Exception:
            pass

    footprint_gib = facts.model_weight_gib * 1.2 + max(1.0, facts.context_length / 4096)
    return max(1, ceil(footprint_gib / (facts.vram_per_gpu_gib * 0.9)))


def _topology_key(candidate: PlannedCandidate) -> tuple[object, ...]:
    return (
        candidate.provider_ref,
        candidate.replicas,
        candidate.tensor_parallel_size,
        candidate.prefill_replicas,
        candidate.prefill_tensor_parallel_size,
        candidate.guide_variant,
        candidate.max_model_len,
    )


class OpenAICompatiblePlanner:
    """Preferred configured planner that can select only candidates validated by Prism."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30,
        provider_type: Literal["openai", "anthropic"] = "openai",
    ) -> None:
        self.settings = OpenAIPlannerSettings(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            provider_type=provider_type,
        )

    @classmethod
    def from_environment(cls) -> OpenAICompatiblePlanner | None:
        base_url = os.getenv("AGENTIC_OPENAI_BASE_URL", "").strip()
        model = os.getenv("AGENTIC_OPENAI_MODEL", "").strip()
        if not base_url or not model:
            return None
        try:
            timeout_seconds = float(os.getenv("AGENTIC_OPENAI_TIMEOUT_SECONDS", "30"))
            return cls(base_url, model, os.getenv("AGENTIC_OPENAI_API_KEY", ""), timeout_seconds)
        except ValueError:
            return None

    @classmethod
    def from_provider_id(cls, provider_id: str | None) -> OpenAICompatiblePlanner | None:
        """Build a planner from a saved External AI Provider, falling back to env config.

        Returns ``None`` (no planner, deterministic fallback applies) when ``provider_id``
        is unset and the environment is also unconfigured, or when the referenced provider
        no longer exists.
        """
        if not provider_id:
            return cls.from_environment()
        from llm_d_bench.ai_providers.service import default_service

        provider = default_service().get(provider_id)
        if provider is None:
            return None
        return cls(
            str(provider.base_url),
            provider.model,
            provider.api_key,
            provider.timeout_seconds,
            provider.provider_type,
        )

    async def recommend(
        self,
        facts: PlanningFacts,
        candidates: list[PlannedCandidate],
        *,
        operator_prompt: str | None = None,
        on_progress: PlannerProgressCallback | None = None,
    ) -> tuple[PlannerDecision, list[PlannedCandidate]]:
        deterministic_planner = DeterministicPlanner()
        valid_candidates = self._diverse_candidate_catalog(facts, candidates)
        if not valid_candidates:
            return deterministic_planner.decide(facts, candidates), []
        valid_candidates = self._diverse_candidate_catalog(facts, valid_candidates)
        valid_ids = {candidate.id for candidate in valid_candidates}
        aic_covered_ids = [
            candidate.id
            for candidate in valid_candidates
            if DeterministicPlanner._aic_prediction(facts, candidate) is not None
        ]
        await self._emit_progress(
            on_progress,
            {
                "phase": "scoring",
                "status": "running",
                "message": (
                    f"Prepared {len(valid_candidates)} validated candidate(s) for AI scoring. "
                    f"AIC has exact estimates for {len(aic_covered_ids)} candidate(s)."
                ),
            },
        )
        planning_facts = asdict(facts)
        planning_facts.pop("performance_score_inputs", None)
        context = {
            "planning_facts": planning_facts,
            "valid_candidates": [self._candidate_context(facts, candidate) for candidate in valid_candidates],
        }
        if operator_prompt:
            context["operator_preference"] = operator_prompt
        user_content = json.dumps(context)
        if operator_prompt:
            user_content += (
                "\n\nOperator preference (primary scoring criterion after hard constraints; "
                "it cannot create configurations):\n" + operator_prompt
            )
        user_content += "\n\nDeployment use case (important scenario-specific planning input):\n" + facts.use_case
        expected_candidate_count = min(3, len(valid_candidates))
        system_prompt = (
            "You are Prism's configuration selector. Follow this priority order:\n"
            "Provider semantics:\n"
            f"{provider_guidance()}\n"
            "1. Choose only among valid_candidates. Never invent IDs, configurations, providers, or facts.\n"
            "2. Consider TTFT, TPOT, and throughput targets, but all supplied candidates remain eligible for scoring. "
            "A null performance_estimate is unknown, not zero. AIC supports only aggregated and disaggregated "
            "topologies; when performance_estimate_applicability is not_applicable, the missing estimate is neutral "
            "and must not lower, exclude, or penalize that guide. Among candidates with comparable estimates that "
            "miss the same latency targets, prioritize the smaller relative SLO violation before GPU savings. "
            "Do not claim a relaxed estimate satisfies the original SLO.\n"
            "3. After hard constraints, operator_preference must be the primary scoring criterion.\n"
            "4. Treat the separately supplied deployment use case and planning_facts workload signals as "
            "scenario-specific ranking guidance when comparing feasible candidates.\n"
            "5. resource_policy is advisory. Resource use includes GPU count, GPU memory pressure, and CPU capacity. "
            "Prefer exact performance_estimate evidence; otherwise use planning_facts and candidate attributes. "
            "Do not let minimum GPU usage override a candidate that better fits significant workload signals. "
            "Use the smallest suitable topology only as a tie-breaker between candidates with comparable workload fit, "
            "or as the default when workload signals are absent or inconclusive. When operator_preference explicitly "
            "prioritizes performance, throughput, or lower latency, rank a feasible higher-replica candidate above the "
            "resource-preferred candidate when it better serves that preference.\n"
            "6. historical_benchmarks are immutable measurements from past runs. match_confidence ranks each record's "
            "relevance to this target; it is not a current performance guarantee. Do not alter their metrics, "
            "configurations, outcomes, or match_confidence. Historical observations are supporting evidence from "
            "similar workloads, not candidate-specific guarantees.\n"
            f"Return exactly {expected_candidate_count} candidate ID(s): return the top three "
            "when at least three valid candidates exist; otherwise return every valid candidate. "
            "Assign scores from 0 to 1. candidate_id must "
            "have the highest score. scores must contain exactly one numeric entry per selected ID. In rationale, "
            "explain why the top candidate scores highest, then name every other returned candidate ID and give its "
            "decisive, candidate-specific reason for receiving a lower score relative to the top candidate. Ground "
            "each reason in the supplied operator preference, workload, topology, resource use, or performance "
            "evidence; do not invent measurements or claim that an unknown estimate fails an SLO. "
            "If only one candidate "
            "is returned, explain its selection without inventing alternatives. Keep rationale concise; avoid repeated "
            "attributes, raw calculations, and unnecessary exact values. "
            "Return only the requested JSON object."
        )
        if self.settings.provider_type == "anthropic":
            system_prompt += (
                " Anthropic response contract: return exactly one valid JSON object with no markdown, prose, or code "
                "fences. It must contain candidate_id (string), candidate_ids (a non-empty JSON array of selected "
                "candidate ID strings), scores (an object with exactly one numeric 0-to-1 score for each candidate_ids "
                "entry), confidence (a numeric 0-to-1 value), and rationale (a string)."
            )
        client = client_for(
            base_url=str(self.settings.base_url),
            model=self.settings.model,
            api_key=self.settings.api_key,
            timeout_seconds=max(self.settings.timeout_seconds, _PLANNER_MIN_TIMEOUT_SECONDS),
            provider_type=self.settings.provider_type,
        )
        json_schema = self._recommendation_schema(valid_ids) if self.settings.provider_type != "anthropic" else None
        recommendation = None
        last_error: Exception | None = None
        for attempt in range(2):
            retry_content = user_content
            if attempt:
                retry_content += (
                    "\n\nYour previous response violated the required JSON contract. "
                    "Return exactly one valid JSON object "
                    "using only the supplied candidate IDs, with matching candidate_ids and scores keys."
                )
            try:
                await self._emit_progress(
                    on_progress,
                    {
                        "phase": "scoring",
                        "status": "running",
                        "message": f"Requesting AI ranking and structured scores (attempt {attempt + 1} of 2).",
                    },
                )
                content = await self._complete_with_progress(
                    client,
                    system_prompt=system_prompt,
                    user_content=retry_content,
                    json_schema=json_schema,
                    max_tokens=_PLANNER_MAX_TOKENS,
                    on_progress=on_progress,
                )
                candidate_recommendation = _parse_recommendation(content)
                self._validate_recommendation(candidate_recommendation, valid_ids)
                self._validate_unmet_slo_ranking(facts, valid_candidates, candidate_recommendation)
                recommendation = candidate_recommendation
                await self._emit_progress(
                    on_progress,
                    {
                        "phase": "scoring",
                        "status": "running",
                        "message": (
                            f"Validated AI scores for {len(candidate_recommendation.candidate_ids)} candidate(s); "
                            f"top choice is {candidate_recommendation.candidate_id}."
                        ),
                    },
                )
                break
            except (AIProviderClientError, KeyError, TypeError, ValueError, OpenAIPlannerError) as error:
                last_error = error
                if attempt == 0:
                    await self._emit_progress(
                        on_progress,
                        {
                            "phase": "scoring",
                            "status": "running",
                            "message": (
                                "The first AI scoring response was invalid; retrying with a stricter JSON instruction."
                            ),
                        },
                    )
        if recommendation is None:
            raise OpenAIPlannerError(f"OpenAI-compatible planner failed: {last_error}") from last_error
        scores = recommendation.scores
        selected_ids = recommendation.candidate_ids
        selected_id = recommendation.candidate_id
        highest_score = max(scores.values())
        normalized_scores = {
            candidate_id: score / highest_score if highest_score > 0 else 1.0 for candidate_id, score in scores.items()
        }
        candidates_by_id = {candidate.id: candidate for candidate in valid_candidates}
        ranked = [
            candidates_by_id[candidate_id].model_copy(
                update={
                    "score": normalized_scores[candidate_id],
                    "score_source": "openai-compatible",
                    "slo_status": deterministic_planner._slo_assessment(facts, candidates_by_id[candidate_id])[0],
                    "slo_confidence": deterministic_planner._slo_assessment(facts, candidates_by_id[candidate_id])[1],
                }
            )
            for candidate_id in selected_ids
        ]
        selected_candidate = next(candidate for candidate in ranked if candidate.id == selected_id)
        slo_status, slo_confidence = deterministic_planner._slo_assessment(facts, selected_candidate)
        return PlannerDecision(
            action="select_candidate",
            candidate_id=selected_id,
            fallback_candidate_ids=[candidate.id for candidate in ranked[1:3]],
            confidence=recommendation.confidence,
            rationale=recommendation.rationale,
            evidence_ids=list(facts.evidence_ids),
            slo_status=slo_status,
            slo_confidence=slo_confidence,
        ), ranked

    @staticmethod
    async def _emit_progress(
        callback: PlannerProgressCallback | None,
        event: dict[str, Any],
    ) -> None:
        if callback is not None:
            await callback(event)

    @classmethod
    async def _complete_with_progress(
        cls,
        client,
        *,
        on_progress: PlannerProgressCallback | None = None,
        **kwargs,
    ) -> str:
        task = asyncio.create_task(client.complete(**kwargs))
        elapsed_seconds = 0
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=10)
                except TimeoutError:
                    elapsed_seconds += 10
                    await cls._emit_progress(
                        on_progress,
                        {
                            "phase": "scoring",
                            "status": "running",
                            "message": f"AI scoring is still running ({elapsed_seconds}s elapsed).",
                        },
                    )
        finally:
            if not task.done():
                task.cancel()

    @staticmethod
    def _validate_recommendation(recommendation: OpenAIPlannerRecommendation, valid_ids: set[str]) -> None:
        selected_ids = recommendation.candidate_ids
        if len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= valid_ids:
            raise OpenAIPlannerError("OpenAI-compatible planner returned invalid candidate selection")
        if recommendation.candidate_id not in valid_ids:
            raise OpenAIPlannerError("OpenAI-compatible planner selected an invalid candidate")
        if set(recommendation.scores) != set(selected_ids):
            raise OpenAIPlannerError("OpenAI-compatible planner returned invalid candidate scores")
        highest_score = max(recommendation.scores.values())
        if recommendation.scores[recommendation.candidate_id] != highest_score:
            raise OpenAIPlannerError("OpenAI-compatible planner selected an invalid candidate")

    @staticmethod
    def _validate_unmet_slo_ranking(
        facts: PlanningFacts,
        candidates: list[PlannedCandidate],
        recommendation: OpenAIPlannerRecommendation,
    ) -> None:
        planner = DeterministicPlanner()
        missed = {
            candidate.id: planner._unmet_slo_priority(facts, candidate)
            for candidate in candidates
            if planner._slo_assessment(facts, candidate)[0] == "not_satisfied"
            and planner._aic_prediction(facts, candidate) is not None
        }
        if not missed:
            return
        selected_missed = [candidate_id for candidate_id in recommendation.candidate_ids if candidate_id in missed]
        if not selected_missed:
            return
        closest = min(missed, key=missed.__getitem__)
        if closest not in selected_missed or any(
            missed[left] < missed[right] and recommendation.scores[left] <= recommendation.scores[right]
            for left in selected_missed
            for right in selected_missed
        ):
            raise OpenAIPlannerError("AI ranking placed a larger known latency SLO miss above a closer estimate")

    @staticmethod
    def _diverse_candidate_catalog(
        facts: PlanningFacts,
        candidates: list[PlannedCandidate],
        *,
        limit: int = 10,
    ) -> list[PlannedCandidate]:
        """Reserve the strongest deployable candidate from each provider before filling the catalog."""
        ranked = DeterministicPlanner().score(facts, candidates)
        selected: list[PlannedCandidate] = []
        selected_providers: set[str] = set()
        for candidate in ranked:
            if candidate.provider_ref not in selected_providers:
                selected.append(candidate)
                selected_providers.add(candidate.provider_ref)
        selected_ids = {candidate.id for candidate in selected}
        selected.extend(candidate for candidate in ranked if candidate.id not in selected_ids)
        return selected[:limit]

    @staticmethod
    def _recommendation_schema(valid_ids: set[str]) -> dict[str, Any]:
        """Constrain provider output to exactly the candidate IDs Prism supplied."""
        schema = OpenAIPlannerRecommendation.model_json_schema()
        ordered_ids = sorted(valid_ids)
        schema["properties"]["candidate_id"] = {"type": "string", "enum": ordered_ids}
        schema["properties"]["candidate_ids"] = {
            "type": "array",
            "items": {"type": "string", "enum": ordered_ids},
            "minItems": 1,
            "maxItems": 10,
        }
        schema["properties"]["scores"] = {
            "type": "object",
            "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
        }
        return schema

    @staticmethod
    def _candidate_context(facts: PlanningFacts, candidate: PlannedCandidate) -> dict[str, Any]:
        """Expose server-validated topology plus an exact AIC estimate when one matches."""
        prediction = DeterministicPlanner._aic_prediction(facts, candidate)
        aic_applicable = candidate.provider_ref in {"baseline-vllm", "optimized-baseline", "pd-disaggregation"}
        performance_estimate = candidate.performance_estimate
        if performance_estimate is None and prediction is not None:
            performance_estimate = {
                "ttft_ms": prediction.ttft_ms,
                "tpot_ms": prediction.tpot_ms,
                "throughput_tokens_per_sec": prediction.throughput_tokens_per_sec,
            }
        return {
            "id": candidate.id,
            "provider_ref": candidate.provider_ref,
            "replicas": candidate.replicas,
            "tensor_parallel_size": candidate.tensor_parallel_size,
            "prefill_replicas": candidate.prefill_replicas,
            "prefill_tensor_parallel_size": candidate.prefill_tensor_parallel_size,
            "guide_variant": candidate.guide_variant,
            "max_model_len": candidate.max_model_len,
            "required_gpus": candidate.required_gpus,
            "performance_estimate_applicability": "applicable" if aic_applicable else "not_applicable",
            "performance_estimate_source": candidate.performance_estimate_source
            or ("aic_search" if prediction else None),
            "performance_estimate": performance_estimate,
            "historical_benchmarks": candidate.historical_benchmarks,
        }
