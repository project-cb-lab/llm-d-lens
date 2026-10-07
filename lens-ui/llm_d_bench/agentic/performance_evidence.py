"""Adapt stored benchmark records into immutable candidate score inputs."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from math import log2

from .benchmark_evidence import (
    BenchmarkEvidenceRetriever,
    BenchmarkQuery,
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
from .planner import PerformanceScoreInput, PlannedCandidate, PlanningFacts, WeightedBenchmarkSample

_MAX_WEIGHTED_SAMPLES = 5


class BenchmarkPerformanceEvidence:
    """Retrieve auditable benchmark evidence for already feasible candidates."""

    def __init__(self, store: JsonBenchmarkRecordStore | None = None) -> None:
        self._store = store or JsonBenchmarkRecordStore()
        self._retriever = BenchmarkEvidenceRetriever()

    def score_inputs(
        self,
        model_name: str,
        facts: PlanningFacts,
        candidates: Iterable[PlannedCandidate],
        *,
        records: list[BenchmarkRecord] | None = None,
    ) -> tuple[PerformanceScoreInput, ...]:
        profile = facts.workload_profile
        if profile is None:
            return ()
        model = ModelDescriptor(
            model_id=model_name,
            family=model_name.split("/")[0],
            architecture="dense",
            parameter_count_b=max(facts.model_weight_gib / 2, 0.1),
            quantization="unknown",
            tokenizer_id=model_name,
        )
        hardware = HardwareDescriptor(
            accelerator="cluster",
            vram_per_gpu_gib=facts.vram_per_gpu_gib,
            gpu_count=max(facts.free_gpu_count, 1),
        )
        runtime = RuntimeFingerprint(
            backend="vllm",
            backend_version="unknown",
            accelerator_runtime="unknown",
        )
        workload = BenchmarkWorkloadProfile(
            mean_input_tokens=profile.mean_input_tokens or facts.context_length,
            p95_input_tokens=profile.p95_input_tokens or profile.mean_input_tokens or facts.context_length,
            mean_output_tokens=profile.mean_output_tokens or 256,
            concurrency=profile.concurrency or 1,
            request_rate=profile.request_rate or 1,
            shared_prefix_ratio=profile.shared_prefix_ratio or 0,
            prefill_fraction=1.0 if facts.prefill_heavy else 0.0,
        )
        score_inputs: list[PerformanceScoreInput] = []
        for candidate in candidates:
            if not candidate.deployable:
                continue
            query = BenchmarkQuery(
                model=model,
                hardware=hardware,
                runtime=runtime,
                workload=workload,
                ttft_slo_ms=facts.ttft_slo_ms,
                tpot_slo_ms=facts.tpot_slo_ms,
                candidate=CandidateConfiguration(
                    topology=candidate.provider_ref,
                    decode_tp=candidate.tensor_parallel_size,
                    decode_replicas=candidate.replicas,
                    prefill_tp=candidate.prefill_tensor_parallel_size,
                    prefill_replicas=candidate.prefill_replicas,
                    optimizations=(candidate.guide_variant or "",),
                ),
            )
            candidate_records = (
                records
                if records is not None
                else self._store.find_compatible(
                    query,
                    observed_after=self._minimum_timestamp(),
                )
            )
            evidence = self._retriever.retrieve(candidate.id, query, candidate_records)
            records_by_id = {record.benchmark_id: record for record in candidate_records}
            score_inputs.append(
                PerformanceScoreInput(
                    candidate_id=candidate.id,
                    source="benchmark",
                    status=evidence.status,
                    ttft_p95_ms=evidence.predicted_metrics.ttft_p95_ms,
                    tpot_p95_ms=evidence.predicted_metrics.tpot_p95_ms,
                    throughput_tokens_per_s=evidence.predicted_metrics.throughput_tokens_per_s,
                    error_rate=evidence.predicted_metrics.error_rate,
                    confidence=evidence.confidence,
                    effective_sample_size=evidence.effective_sample_size,
                    has_blocking_negative_evidence=(
                        bool(evidence.negative_evidence)
                        and evidence.matching_runs >= self._retriever.min_effective_samples
                    ),
                    evidence_refs=tuple(f"benchmark:{reference}" for reference in evidence.benchmark_refs),
                    weighted_samples=tuple(
                        WeightedBenchmarkSample(
                            benchmark_id=sample.benchmark_id,
                            similarity=sample.similarity,
                            time_decay=sample.time_decay,
                            runtime_similarity=sample.runtime_similarity,
                            match_confidence=sample.weight,
                            observed_at=record.timestamp.isoformat(),
                            outcome=record.outcome,
                            ttft_p95_ms=record.metrics.ttft_p95_ms,
                            tpot_p95_ms=record.metrics.tpot_p95_ms,
                            throughput_tokens_per_s=record.metrics.throughput_tokens_per_s,
                            error_rate=record.metrics.error_rate,
                            failure_reason=record.failure_reason,
                            topology=record.configuration.topology,
                            decode_tp=record.configuration.decode_tp,
                            decode_replicas=record.configuration.decode_replicas,
                            prefill_tp=record.configuration.prefill_tp,
                            prefill_replicas=record.configuration.prefill_replicas,
                            optimizations=record.configuration.optimizations,
                        )
                        for sample in sorted(
                            evidence.weighted_benchmarks,
                            key=lambda item: (-item.weight, item.benchmark_id),
                        )[:_MAX_WEIGHTED_SAMPLES]
                        for record in (records_by_id[sample.benchmark_id],)
                    ),
                )
            )
        return tuple(score_inputs)

    def historical_samples(
        self,
        model_name: str,
        facts: PlanningFacts,
        candidates: Iterable[PlannedCandidate],
        *,
        records: list[BenchmarkRecord] | None = None,
    ) -> dict[str, tuple[WeightedBenchmarkSample, ...]]:
        """Return bounded raw samples for AI context without computing score inputs."""
        profile = facts.workload_profile
        if profile is None:
            return {}
        model = ModelDescriptor(
            model_id=model_name,
            family=model_name.split("/")[0],
            architecture="dense",
            parameter_count_b=max(facts.model_weight_gib / 2, 0.1),
            quantization="unknown",
            tokenizer_id=model_name,
        )
        hardware = HardwareDescriptor(
            accelerator="cluster",
            vram_per_gpu_gib=facts.vram_per_gpu_gib,
            gpu_count=max(facts.free_gpu_count, 1),
        )
        runtime = RuntimeFingerprint(
            backend="vllm",
            backend_version="unknown",
            accelerator_runtime="unknown",
        )
        workload = BenchmarkWorkloadProfile(
            mean_input_tokens=profile.mean_input_tokens or facts.context_length,
            p95_input_tokens=profile.p95_input_tokens or profile.mean_input_tokens or facts.context_length,
            mean_output_tokens=profile.mean_output_tokens or 256,
            concurrency=profile.concurrency or 1,
            request_rate=profile.request_rate or 1,
            shared_prefix_ratio=profile.shared_prefix_ratio or 0,
            prefill_fraction=1.0 if facts.prefill_heavy else 0.0,
        )
        samples: dict[str, tuple[WeightedBenchmarkSample, ...]] = {}
        for candidate in candidates:
            if not candidate.deployable:
                continue
            query = BenchmarkQuery(
                model=model,
                hardware=hardware,
                runtime=runtime,
                workload=workload,
                ttft_slo_ms=facts.ttft_slo_ms,
                tpot_slo_ms=facts.tpot_slo_ms,
                candidate=CandidateConfiguration(
                    topology=candidate.provider_ref,
                    decode_tp=candidate.tensor_parallel_size,
                    decode_replicas=candidate.replicas,
                    prefill_tp=candidate.prefill_tensor_parallel_size,
                    prefill_replicas=candidate.prefill_replicas,
                    optimizations=(candidate.guide_variant or "",),
                ),
            )
            candidate_records = (
                records
                if records is not None
                else self._store.find_compatible(
                    query,
                    observed_after=self._minimum_timestamp(),
                )
            )
            matches = self._retriever.matching_benchmarks(query, candidate_records)
            samples[candidate.id] = tuple(
                WeightedBenchmarkSample(
                    benchmark_id=record.benchmark_id,
                    similarity=match.similarity,
                    time_decay=match.time_decay,
                    runtime_similarity=match.runtime_similarity,
                    match_confidence=match.weight,
                    observed_at=record.timestamp.isoformat(),
                    outcome=record.outcome,
                    ttft_p95_ms=record.metrics.ttft_p95_ms,
                    tpot_p95_ms=record.metrics.tpot_p95_ms,
                    throughput_tokens_per_s=record.metrics.throughput_tokens_per_s,
                    error_rate=record.metrics.error_rate,
                    failure_reason=record.failure_reason,
                    topology=record.configuration.topology,
                    decode_tp=record.configuration.decode_tp,
                    decode_replicas=record.configuration.decode_replicas,
                    prefill_tp=record.configuration.prefill_tp,
                    prefill_replicas=record.configuration.prefill_replicas,
                    optimizations=record.configuration.optimizations,
                )
                for record, match in sorted(matches, key=lambda item: (-item[1].weight, item[0].benchmark_id))[
                    :_MAX_WEIGHTED_SAMPLES
                ]
            )
        return samples

    def _minimum_timestamp(self) -> datetime:
        """Return the lossless time cutoff implied by the retriever policy."""
        maximum_age_days = self._retriever.half_life_days * (-log2(self._retriever.min_weight))
        return datetime.now(UTC) - timedelta(days=maximum_age_days)
