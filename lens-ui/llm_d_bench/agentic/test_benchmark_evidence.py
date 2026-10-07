"""Tests for deterministic benchmark-history weighting."""

from datetime import UTC, datetime, timedelta

import pytest

from llm_d_bench.agentic.benchmark_evidence import (
    BenchmarkEvidenceRetriever,
    BenchmarkMetrics,
    BenchmarkQuery,
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
    WorkloadProfile,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _query() -> BenchmarkQuery:
    return BenchmarkQuery(
        model=ModelDescriptor(
            model_id="qwen3-8b",
            family="Qwen3",
            architecture="dense",
            parameter_count_b=8,
            quantization="bf16",
            tokenizer_id="qwen3",
        ),
        hardware=HardwareDescriptor(accelerator="Gaudi3", vram_per_gpu_gib=96, gpu_count=4),
        runtime=RuntimeFingerprint(backend="vllm", backend_version="0.8.5", accelerator_runtime="1.20"),
        workload=WorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=8192,
            mean_output_tokens=512,
            concurrency=16,
            request_rate=4,
            shared_prefix_ratio=0.7,
            prefill_fraction=0.8,
        ),
        candidate=CandidateConfiguration(
            topology="pd-disaggregation",
            decode_tp=2,
            decode_replicas=2,
            prefill_tp=2,
            prefill_replicas=2,
            optimizations=("tiered-prefix-cache",),
        ),
    )


def _record(
    benchmark_id: str,
    *,
    days_old: int = 0,
    outcome: str = "succeeded",
    failure_reason: str | None = None,
    ttft: float = 400,
) -> BenchmarkRecord:
    query = _query()
    return BenchmarkRecord(
        benchmark_id=benchmark_id,
        timestamp=NOW - timedelta(days=days_old),
        outcome=outcome,
        model=query.model,
        hardware=query.hardware,
        runtime=query.runtime,
        workload=query.workload,
        configuration=query.candidate,
        metrics=BenchmarkMetrics(ttft_p95_ms=ttft, tpot_p95_ms=30, throughput_tokens_per_s=800, error_rate=0.01),
        failure_reason=failure_reason,
    )


def test_model_similarity_uses_architecture_quantization_and_parameter_scale():
    retriever = BenchmarkEvidenceRetriever()
    query = _query().model
    same = retriever.model_similarity(query, query)
    doubled = retriever.model_similarity(query.model_copy(update={"parameter_count_b": 16}), query)
    moe = retriever.model_similarity(
        query.model_copy(update={"architecture": "moe", "active_parameter_count_b": 8}), query
    )
    quantized = retriever.model_similarity(query.model_copy(update={"quantization": "fp8"}), query)

    assert same == 1.0
    assert 0.8 < doubled < same
    assert moe < 0.7
    assert quantized == 0.0


def test_model_similarity_distinguishes_family_tokenizer_and_moe_active_parameters():
    retriever = BenchmarkEvidenceRetriever()
    query = _query().model
    same_family_different_tokenizer = retriever.model_similarity(
        query.model_copy(update={"tokenizer_id": "qwen3-other"}),
        query,
    )
    different_family = retriever.model_similarity(
        query.model_copy(update={"family": "Llama"}),
        query,
    )
    moe_query = query.model_copy(
        update={
            "architecture": "moe",
            "parameter_count_b": 128,
            "active_parameter_count_b": 8,
        }
    )
    same_active_parameters = retriever.model_similarity(
        moe_query.model_copy(update={"parameter_count_b": 256}),
        moe_query,
    )
    different_active_parameters = retriever.model_similarity(
        moe_query.model_copy(update={"active_parameter_count_b": 32}),
        moe_query,
    )

    assert 0.9 < same_family_different_tokenizer < 1.0
    assert different_family < same_family_different_tokenizer
    assert same_active_parameters == 1.0
    assert different_active_parameters < same_active_parameters


def test_retriever_aggregates_weighted_metrics_and_negative_evidence():
    evidence = BenchmarkEvidenceRetriever().retrieve(
        "candidate-a",
        _query(),
        [
            _record("recent", ttft=400),
            _record("old", days_old=30, ttft=600),
            _record("oom", outcome="failed", failure_reason="OOM"),
        ],
        now=NOW,
    )

    assert evidence.matching_runs == 3
    assert evidence.predicted_metrics.ttft_p95_ms == pytest.approx(466.67, abs=0.01)
    assert evidence.effective_sample_size == pytest.approx(1.8, abs=0.01)
    assert evidence.status == "interpolated"
    assert evidence.negative_evidence == ["OOM"]
    assert evidence.confidence < 0.5


def test_retriever_excludes_incompatible_or_low_weight_records():
    incompatible = _record("wrong-backend")
    incompatible.runtime = incompatible.runtime.model_copy(update={"backend": "sglang"})
    stale = _record("stale", days_old=365)

    evidence = BenchmarkEvidenceRetriever().retrieve("candidate-a", _query(), [incompatible, stale], now=NOW)

    assert evidence.status == "unknown"
    assert evidence.matching_runs == 0
    assert evidence.benchmark_refs == []


def test_retriever_preserves_time_runtime_and_model_relevance_for_mixed_history():
    query = _query()
    recent_exact = _record("recent-exact")
    old_runtime_patch = _record("old-runtime-patch", days_old=30)
    old_runtime_patch.runtime = old_runtime_patch.runtime.model_copy(update={"backend_version": "0.8.6"})
    nearby_model = _record("nearby-model")
    nearby_model.model = nearby_model.model.model_copy(update={"parameter_count_b": 16})
    wrong_quantization = _record("wrong-quantization")
    wrong_quantization.model = wrong_quantization.model.model_copy(update={"quantization": "fp8"})

    evidence = BenchmarkEvidenceRetriever().retrieve(
        "candidate-a",
        query,
        [old_runtime_patch, nearby_model, wrong_quantization, recent_exact],
        now=NOW,
    )
    weights = {item.benchmark_id: item for item in evidence.weighted_benchmarks}

    assert evidence.benchmark_refs == ["old-runtime-patch", "nearby-model", "recent-exact"]
    assert "wrong-quantization" not in weights
    assert weights["recent-exact"].similarity == 1.0
    assert weights["recent-exact"].time_decay == 1.0
    assert weights["recent-exact"].runtime_similarity == 1.0
    assert weights["old-runtime-patch"].time_decay == pytest.approx(0.5)
    assert weights["old-runtime-patch"].runtime_similarity == pytest.approx(0.93)
    assert weights["nearby-model"].similarity < weights["recent-exact"].similarity
    assert weights["recent-exact"].weight > weights["nearby-model"].weight > weights["old-runtime-patch"].weight
