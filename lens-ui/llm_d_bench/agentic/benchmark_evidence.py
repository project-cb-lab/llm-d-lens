"""Deterministic, auditable aggregation of normalized benchmark history."""

from __future__ import annotations

from datetime import UTC, datetime
from math import exp, log
from typing import Literal

from pydantic import BaseModel, Field


class ModelDescriptor(BaseModel):
    model_id: str
    family: str
    architecture: Literal["dense", "moe"]
    parameter_count_b: float = Field(gt=0)
    active_parameter_count_b: float | None = Field(default=None, gt=0)
    quantization: str
    tokenizer_id: str
    revision: str | None = None


class RuntimeFingerprint(BaseModel):
    backend: str
    backend_version: str
    accelerator_runtime: str
    driver_version: str | None = None
    container_digest: str | None = None
    git_revision: str | None = None


class HardwareDescriptor(BaseModel):
    accelerator: str
    vram_per_gpu_gib: float = Field(gt=0)
    gpu_count: int = Field(ge=1)


class WorkloadProfile(BaseModel):
    mean_input_tokens: float = Field(gt=0)
    p95_input_tokens: float = Field(gt=0)
    mean_output_tokens: float = Field(gt=0)
    concurrency: float = Field(gt=0)
    request_rate: float = Field(gt=0)
    shared_prefix_ratio: float = Field(ge=0, le=1)
    prefill_fraction: float = Field(ge=0, le=1)


class CandidateConfiguration(BaseModel):
    topology: str
    decode_tp: int = Field(ge=1)
    decode_replicas: int = Field(ge=1)
    prefill_tp: int | None = Field(default=None, ge=1)
    prefill_replicas: int | None = Field(default=None, ge=1)
    optimizations: tuple[str, ...] = ()


class BenchmarkMetrics(BaseModel):
    ttft_p95_ms: float | None = Field(default=None, gt=0)
    tpot_p95_ms: float | None = Field(default=None, gt=0)
    e2e_p95_ms: float | None = Field(default=None, gt=0)
    throughput_tokens_per_s: float | None = Field(default=None, gt=0)
    error_rate: float | None = Field(default=None, ge=0, le=1)


class BenchmarkRecord(BaseModel):
    benchmark_id: str
    timestamp: datetime
    outcome: Literal["succeeded", "failed"]
    model: ModelDescriptor
    hardware: HardwareDescriptor
    runtime: RuntimeFingerprint
    workload: WorkloadProfile
    configuration: CandidateConfiguration
    metrics: BenchmarkMetrics = Field(default_factory=BenchmarkMetrics)
    failure_reason: str | None = None


class BenchmarkQuery(BaseModel):
    model: ModelDescriptor
    hardware: HardwareDescriptor
    runtime: RuntimeFingerprint
    workload: WorkloadProfile
    candidate: CandidateConfiguration
    ttft_slo_ms: float | None = Field(default=None, gt=0)
    tpot_slo_ms: float | None = Field(default=None, gt=0)


class WeightedBenchmark(BaseModel):
    benchmark_id: str
    similarity: float = Field(ge=0, le=1)
    time_decay: float = Field(ge=0, le=1)
    runtime_similarity: float = Field(ge=0, le=1)
    weight: float = Field(ge=0, le=1)


class CandidatePredictionEvidence(BaseModel):
    candidate_id: str
    status: Literal["measured", "interpolated", "estimated", "unknown"]
    matching_runs: int = Field(ge=0)
    effective_sample_size: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    predicted_metrics: BenchmarkMetrics = Field(default_factory=BenchmarkMetrics)
    negative_evidence: list[str] = Field(default_factory=list)
    benchmark_refs: list[str] = Field(default_factory=list)
    weighted_benchmarks: list[WeightedBenchmark] = Field(default_factory=list)


class BenchmarkEvidenceRetriever:
    """Weight normalized history for one already-feasible candidate configuration."""

    min_similarity = 0.70
    min_weight = 0.20
    min_effective_samples = 3.0
    half_life_days = 30.0
    _threshold_tolerance = 1e-9

    def retrieve(
        self,
        candidate_id: str,
        query: BenchmarkQuery,
        records: list[BenchmarkRecord],
        *,
        now: datetime | None = None,
    ) -> CandidatePredictionEvidence:
        matches = self.matching_benchmarks(query, records, now=now)
        successful = [(record, match) for record, match in matches if record.outcome == "succeeded"]
        effective_sample_size = self._effective_sample_size([match.weight for _, match in successful])
        negative_evidence = sorted(
            {record.failure_reason or "benchmark failed" for record, _ in matches if record.outcome == "failed"}
        )
        if not matches:
            status: Literal["measured", "interpolated", "estimated", "unknown"] = "unknown"
        elif effective_sample_size + self._threshold_tolerance >= self.min_effective_samples and not negative_evidence:
            status = "measured"
        else:
            status = "interpolated"
        confidence = min(1.0, effective_sample_size / self.min_effective_samples)
        if negative_evidence:
            confidence *= 0.5
        return CandidatePredictionEvidence(
            candidate_id=candidate_id,
            status=status,
            matching_runs=len(matches),
            effective_sample_size=effective_sample_size,
            confidence=confidence,
            predicted_metrics=self._aggregate_metrics(successful),
            negative_evidence=negative_evidence,
            benchmark_refs=[match.benchmark_id for _, match in matches],
            weighted_benchmarks=[match for _, match in matches],
        )

    def matching_benchmarks(
        self,
        query: BenchmarkQuery,
        records: list[BenchmarkRecord],
        *,
        now: datetime | None = None,
    ) -> list[tuple[BenchmarkRecord, WeightedBenchmark]]:
        """Return compatible raw records with relevance weights, without aggregation."""
        now = now or datetime.now(UTC)
        matches: list[tuple[BenchmarkRecord, WeightedBenchmark]] = []
        for record in records:
            if not self._compatible(record, query):
                continue
            similarity = self.similarity(record, query)
            time_decay = self.time_decay(record.timestamp, now)
            runtime_similarity = self.runtime_similarity(record.runtime, query.runtime)
            weight = similarity * time_decay * runtime_similarity
            if similarity < self.min_similarity or weight < self.min_weight:
                continue
            matches.append(
                (
                    record,
                    WeightedBenchmark(
                        benchmark_id=record.benchmark_id,
                        similarity=similarity,
                        time_decay=time_decay,
                        runtime_similarity=runtime_similarity,
                        weight=weight,
                    ),
                )
            )
        return matches

    def similarity(self, record: BenchmarkRecord, query: BenchmarkQuery) -> float:
        conditions = (
            0.30 * self.model_similarity(record.model, query.model)
            + 0.25 * self.hardware_similarity(record.hardware, query.hardware)
            + 0.30 * self.workload_similarity(record.workload, query.workload)
            + 0.15 * self.configuration_similarity(record.configuration, query.candidate)
        )
        targets = (
            (record.metrics.ttft_p95_ms, query.ttft_slo_ms),
            (record.metrics.tpot_p95_ms, query.tpot_slo_ms),
        )
        requested = [(actual, target) for actual, target in targets if target is not None]
        if record.outcome != "succeeded" or not requested or any(actual is None for actual, _ in requested):
            return conditions
        slo_fit = sum(min(1.0, target / actual) for actual, target in requested) / len(requested)
        return conditions * (0.6 + 0.4 * slo_fit)

    @staticmethod
    def model_similarity(record: ModelDescriptor, query: ModelDescriptor) -> float:
        if record.quantization != query.quantization:
            return 0.0
        if record.architecture != query.architecture:
            return 0.0
        parameter_record = BenchmarkEvidenceRetriever._compute_parameters(record)
        parameter_query = BenchmarkEvidenceRetriever._compute_parameters(query)
        parameter_score = exp(-0.8 * abs(log(parameter_record / parameter_query)))
        similarity = (
            0.45 * (1.0 if record.family == query.family else 0.15)
            + 0.25
            + 0.20 * parameter_score
            + 0.10 * (1.0 if record.tokenizer_id == query.tokenizer_id else 0.5)
        )
        return round(min(1.0, similarity), 12)

    @staticmethod
    def runtime_similarity(record: RuntimeFingerprint, query: RuntimeFingerprint) -> float:
        if record.backend != query.backend:
            return 0.0
        record_version = BenchmarkEvidenceRetriever._version_parts(record.backend_version)
        query_version = BenchmarkEvidenceRetriever._version_parts(query.backend_version)
        if record_version[0] != query_version[0]:
            backend_score = 0.2
        elif record_version[1] != query_version[1]:
            backend_score = 0.65
        elif record_version[2] != query_version[2]:
            backend_score = 0.9
        else:
            backend_score = 1.0
        accelerator_score = 1.0 if record.accelerator_runtime == query.accelerator_runtime else 0.6
        return 0.7 * backend_score + 0.3 * accelerator_score

    def time_decay(self, timestamp: datetime, now: datetime) -> float:
        timestamp = self._as_utc(timestamp)
        now = self._as_utc(now)
        age_days = max(0.0, (now - timestamp).total_seconds() / 86_400)
        return exp(-log(2) * age_days / self.half_life_days)

    @staticmethod
    def _compatible(record: BenchmarkRecord, query: BenchmarkQuery) -> bool:
        return (
            record.hardware.accelerator == query.hardware.accelerator
            and record.runtime.backend == query.runtime.backend
            and record.model.quantization == query.model.quantization
            and record.hardware.vram_per_gpu_gib >= query.hardware.vram_per_gpu_gib
            and record.configuration.topology == query.candidate.topology
        )

    @staticmethod
    def hardware_similarity(record: HardwareDescriptor, query: HardwareDescriptor) -> float:
        return 0.7 + 0.3 * exp(-abs(log(record.gpu_count / query.gpu_count)))

    @staticmethod
    def workload_similarity(record: WorkloadProfile, query: WorkloadProfile) -> float:
        ratio_scores = [
            exp(-abs(log(record.mean_input_tokens / query.mean_input_tokens))),
            exp(-abs(log(record.p95_input_tokens / query.p95_input_tokens))),
            exp(-abs(log(record.mean_output_tokens / query.mean_output_tokens))),
            exp(-abs(log(record.concurrency / query.concurrency))),
            exp(-abs(log(record.request_rate / query.request_rate))),
            1 - abs(record.shared_prefix_ratio - query.shared_prefix_ratio),
            1 - abs(record.prefill_fraction - query.prefill_fraction),
        ]
        return sum(ratio_scores) / len(ratio_scores)

    @staticmethod
    def configuration_similarity(record: CandidateConfiguration, query: CandidateConfiguration) -> float:
        optimization_union = set(record.optimizations) | set(query.optimizations)
        optimization_score = (
            1.0
            if not optimization_union
            else len(set(record.optimizations) & set(query.optimizations)) / len(optimization_union)
        )
        return (
            0.4 * exp(-abs(log(record.decode_tp / query.decode_tp)))
            + 0.4 * exp(-abs(log(record.decode_replicas / query.decode_replicas)))
            + 0.2 * optimization_score
        )

    @staticmethod
    def _aggregate_metrics(matches: list[tuple[BenchmarkRecord, WeightedBenchmark]]) -> BenchmarkMetrics:
        def average(field: str) -> float | None:
            values = [(getattr(record.metrics, field), match.weight) for record, match in matches]
            values = [(value, weight) for value, weight in values if value is not None]
            return (
                None
                if not values
                else sum(value * weight for value, weight in values) / sum(weight for _, weight in values)
            )

        return BenchmarkMetrics(
            ttft_p95_ms=average("ttft_p95_ms"),
            tpot_p95_ms=average("tpot_p95_ms"),
            e2e_p95_ms=average("e2e_p95_ms"),
            throughput_tokens_per_s=average("throughput_tokens_per_s"),
            error_rate=average("error_rate"),
        )

    @staticmethod
    def _compute_parameters(model: ModelDescriptor) -> float:
        return (
            model.active_parameter_count_b
            if model.architecture == "moe" and model.active_parameter_count_b
            else model.parameter_count_b
        )

    @staticmethod
    def _effective_sample_size(weights: list[float]) -> float:
        return 0.0 if not weights else sum(weights) ** 2 / sum(weight**2 for weight in weights)

    @staticmethod
    def _version_parts(version: str) -> tuple[int, int, int]:
        numeric = version.split("+", 1)[0].split("-", 1)[0].lstrip("v").split(".")
        return tuple(int(part) if part.isdigit() else 0 for part in (numeric + ["0", "0"])[:3])

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
