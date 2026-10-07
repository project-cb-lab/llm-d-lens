"""Tests for benchmark evidence adaptation into deterministic score inputs."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from llm_d_bench.agentic.benchmark_evidence import (
    BenchmarkMetrics,
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
)
from llm_d_bench.agentic.benchmark_evidence import (
    WorkloadProfile as BenchmarkWorkloadProfile,
)
from llm_d_bench.agentic.benchmark_store import JsonBenchmarkRecordStore
from llm_d_bench.agentic.performance_evidence import BenchmarkPerformanceEvidence
from llm_d_bench.agentic.planner import (
    DeterministicPlanner,
    OpenAICompatiblePlanner,
    PlanningFacts,
    WorkloadProfile,
)


def test_benchmark_performance_evidence_maps_matching_records_to_candidate_inputs(tmp_path):
    facts = PlanningFacts(
        model_weight_gib=16,
        vram_per_gpu_gib=96,
        free_gpu_count=2,
        workload_profile=WorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=8192,
            mean_output_tokens=512,
            concurrency=16,
            request_rate=4,
            shared_prefix_ratio=0.7,
        ),
    )
    candidate = next(item for item in DeterministicPlanner().candidates(facts) if item.id == "baseline-vllm-tp1-r1")
    record = BenchmarkRecord(
        benchmark_id="bench-1",
        timestamp=datetime.now(UTC),
        outcome="succeeded",
        model=ModelDescriptor(
            model_id="Qwen/Qwen3-8B",
            family="Qwen",
            architecture="dense",
            parameter_count_b=8,
            quantization="unknown",
            tokenizer_id="Qwen/Qwen3-8B",
        ),
        hardware=HardwareDescriptor(accelerator="cluster", vram_per_gpu_gib=96, gpu_count=2),
        runtime=RuntimeFingerprint(backend="vllm", backend_version="unknown", accelerator_runtime="unknown"),
        workload=BenchmarkWorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=8192,
            mean_output_tokens=512,
            concurrency=16,
            request_rate=4,
            shared_prefix_ratio=0.7,
            prefill_fraction=0,
        ),
        configuration=CandidateConfiguration(
            topology="baseline-vllm", decode_tp=1, decode_replicas=1, optimizations=("",)
        ),
        metrics=BenchmarkMetrics(ttft_p95_ms=300, tpot_p95_ms=25, throughput_tokens_per_s=700, error_rate=0.01),
    )
    evidence = BenchmarkPerformanceEvidence(JsonBenchmarkRecordStore(tmp_path)).score_inputs(
        "Qwen/Qwen3-8B",
        facts,
        [candidate],
        records=[record],
    )

    assert len(evidence) == 1
    assert evidence[0].candidate_id == candidate.id
    assert evidence[0].status == "interpolated"
    assert evidence[0].ttft_p95_ms == 300
    assert evidence[0].evidence_refs == ("benchmark:bench-1",)
    sample = evidence[0].weighted_samples[0]
    assert sample.benchmark_id == "bench-1"
    assert sample.match_confidence > 0.99
    assert sample.ttft_p95_ms == 300
    assert sample.outcome == "succeeded"
    assert sample.topology == "baseline-vllm"


def test_benchmark_performance_evidence_blocks_repeated_matching_failures(tmp_path):
    facts = PlanningFacts(
        model_weight_gib=16,
        vram_per_gpu_gib=96,
        free_gpu_count=2,
        workload_profile=WorkloadProfile(mean_input_tokens=4096),
    )
    candidate = next(item for item in DeterministicPlanner().candidates(facts) if item.id == "baseline-vllm-tp1-r1")
    common = {
        "timestamp": datetime.now(UTC),
        "outcome": "failed",
        "model": ModelDescriptor(
            model_id="Qwen/Qwen3-8B",
            family="Qwen",
            architecture="dense",
            parameter_count_b=8,
            quantization="unknown",
            tokenizer_id="Qwen/Qwen3-8B",
        ),
        "hardware": HardwareDescriptor(accelerator="cluster", vram_per_gpu_gib=96, gpu_count=2),
        "runtime": RuntimeFingerprint(backend="vllm", backend_version="unknown", accelerator_runtime="unknown"),
        "workload": BenchmarkWorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=4096,
            mean_output_tokens=256,
            concurrency=1,
            request_rate=1,
            shared_prefix_ratio=0,
            prefill_fraction=0,
        ),
        "configuration": CandidateConfiguration(
            topology="baseline-vllm", decode_tp=1, decode_replicas=1, optimizations=("",)
        ),
        "failure_reason": "OOM",
    }
    records = [BenchmarkRecord(benchmark_id=f"failed-{index}", **common) for index in range(3)]
    evidence = BenchmarkPerformanceEvidence(JsonBenchmarkRecordStore(tmp_path)).score_inputs(
        "Qwen/Qwen3-8B",
        facts,
        [candidate],
        records=records,
    )

    assert evidence[0].has_blocking_negative_evidence


def test_persisted_benchmark_history_flows_to_planner_and_ai_context(tmp_path):
    facts = PlanningFacts(
        model_weight_gib=16,
        vram_per_gpu_gib=96,
        free_gpu_count=2,
        ttft_slo_ms=500,
        tpot_slo_ms=50,
        workload_profile=WorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=8192,
            mean_output_tokens=512,
            concurrency=16,
            request_rate=4,
            shared_prefix_ratio=0.7,
        ),
    )
    deterministic_planner = DeterministicPlanner()
    candidates = deterministic_planner.candidates(facts)
    baseline = next(candidate for candidate in candidates if candidate.id == "baseline-vllm-tp1-r1")
    pd = next(candidate for candidate in candidates if candidate.provider_ref == "pd-disaggregation")
    store = JsonBenchmarkRecordStore(tmp_path)
    common = {
        "timestamp": datetime.now(UTC),
        "model": ModelDescriptor(
            model_id="Qwen/Qwen3-8B",
            family="Qwen",
            architecture="dense",
            parameter_count_b=8,
            quantization="unknown",
            tokenizer_id="Qwen/Qwen3-8B",
        ),
        "hardware": HardwareDescriptor(accelerator="cluster", vram_per_gpu_gib=96, gpu_count=2),
        "runtime": RuntimeFingerprint(backend="vllm", backend_version="unknown", accelerator_runtime="unknown"),
        "workload": BenchmarkWorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=8192,
            mean_output_tokens=512,
            concurrency=16,
            request_rate=4,
            shared_prefix_ratio=0.7,
            prefill_fraction=0,
        ),
    }
    baseline_configuration = CandidateConfiguration(
        topology=baseline.provider_ref,
        decode_tp=baseline.tensor_parallel_size,
        decode_replicas=baseline.replicas,
        optimizations=(baseline.guide_variant or "",),
    )
    pd_configuration = CandidateConfiguration(
        topology=pd.provider_ref,
        decode_tp=pd.tensor_parallel_size,
        decode_replicas=pd.replicas,
        prefill_tp=pd.prefill_tensor_parallel_size,
        prefill_replicas=pd.prefill_replicas,
        optimizations=(pd.guide_variant or "",),
    )
    for index in range(3):
        store.save(
            BenchmarkRecord(
                benchmark_id=f"baseline-success-{index}",
                outcome="succeeded",
                configuration=baseline_configuration,
                metrics=BenchmarkMetrics(
                    ttft_p95_ms=300,
                    tpot_p95_ms=25,
                    throughput_tokens_per_s=700,
                    error_rate=0.01,
                ),
                **common,
            )
        )
        store.save(
            BenchmarkRecord(
                benchmark_id=f"pd-failure-{index}",
                outcome="failed",
                configuration=pd_configuration,
                failure_reason="OOM",
                **common,
            )
        )
    store.save(
        BenchmarkRecord(
            benchmark_id="ignored-other-accelerator",
            outcome="succeeded",
            configuration=baseline_configuration,
            hardware=HardwareDescriptor(accelerator="other", vram_per_gpu_gib=96, gpu_count=2),
            metrics=BenchmarkMetrics(ttft_p95_ms=1, tpot_p95_ms=1, throughput_tokens_per_s=9999),
            **{key: value for key, value in common.items() if key != "hardware"},
        )
    )

    score_inputs = BenchmarkPerformanceEvidence(store).score_inputs(
        "Qwen/Qwen3-8B",
        facts,
        [baseline, pd],
    )
    inputs_by_candidate = {item.candidate_id: item for item in score_inputs}
    baseline_evidence = inputs_by_candidate[baseline.id]
    pd_evidence = inputs_by_candidate[pd.id]

    assert baseline_evidence.status == "measured"
    assert baseline_evidence.ttft_p95_ms == pytest.approx(300)
    assert baseline_evidence.tpot_p95_ms == pytest.approx(25)
    assert baseline_evidence.throughput_tokens_per_s == pytest.approx(700)
    assert baseline_evidence.evidence_refs == tuple(f"benchmark:baseline-success-{index}" for index in range(3))
    assert [sample.benchmark_id for sample in baseline_evidence.weighted_samples] == [
        f"baseline-success-{index}" for index in range(3)
    ]
    assert pd_evidence.has_blocking_negative_evidence
    assert pd_evidence.evidence_refs == tuple(f"benchmark:pd-failure-{index}" for index in range(3))

    facts_with_evidence = replace(facts, performance_score_inputs=score_inputs)
    candidates_with_evidence = deterministic_planner.candidates(facts_with_evidence)
    decision = deterministic_planner.decide(facts_with_evidence, candidates_with_evidence)
    assert decision.candidate_id == baseline.id
    blocked_pd = next(candidate for candidate in candidates_with_evidence if candidate.id == pd.id)
    assert not blocked_pd.deployable
    assert "blocking benchmark failure evidence" in blocked_pd.rejection_reasons
    assert [
        sample["benchmark_id"]
        for sample in next(
            candidate for candidate in candidates_with_evidence if candidate.id == baseline.id
        ).historical_benchmarks
    ] == [f"baseline-success-{index}" for index in range(3)]
    assert [sample["failure_reason"] for sample in blocked_pd.historical_benchmarks] == ["OOM"] * 3

    baseline_with_evidence = next(candidate for candidate in candidates_with_evidence if candidate.id == baseline.id)
    ai_context = OpenAICompatiblePlanner("http://provider", "planner")._candidate_context(
        facts_with_evidence,
        baseline_with_evidence,
    )
    assert "benchmark_evidence" not in ai_context
    assert [sample["benchmark_id"] for sample in ai_context["historical_benchmarks"]] == [
        f"baseline-success-{index}" for index in range(3)
    ]
    assert all(sample["match_confidence"] > 0.99 for sample in ai_context["historical_benchmarks"])
