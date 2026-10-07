"""Tests for benchmark-backed candidate seed discovery."""

from datetime import UTC, datetime

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
from llm_d_bench.agentic.historical_candidate_seeds import HistoricalCandidateSeedRetriever
from llm_d_bench.agentic.planner import PlanningFacts, WorkloadProfile


def _record(benchmark_id: str, configuration: CandidateConfiguration) -> BenchmarkRecord:
    return BenchmarkRecord(
        benchmark_id=benchmark_id,
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
        configuration=configuration,
        metrics=BenchmarkMetrics(ttft_p95_ms=300, tpot_p95_ms=25, throughput_tokens_per_s=700),
    )


def test_retriever_returns_qualified_cross_topology_configuration_as_seed(tmp_path):
    store = JsonBenchmarkRecordStore(tmp_path)
    configuration = CandidateConfiguration(
        topology="pd-disaggregation",
        decode_tp=1,
        decode_replicas=1,
        prefill_tp=1,
        prefill_replicas=1,
    )
    for index in range(3):
        store.save(_record(f"pd-{index}", configuration))
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

    seeds = HistoricalCandidateSeedRetriever(store).retrieve("Qwen/Qwen3-8B", facts)

    assert len(seeds) == 1
    assert seeds[0].proposal.provider_ref == "pd-disaggregation"
    assert seeds[0].proposal.prefill_tensor_parallel_size == 1
    assert seeds[0].evidence_refs == ("benchmark:pd-0", "benchmark:pd-1", "benchmark:pd-2")


def test_retriever_handles_mixed_nullable_prefill_configurations(tmp_path):
    store = JsonBenchmarkRecordStore(tmp_path)
    for index in range(3):
        store.save(
            _record(
                f"baseline-{index}",
                CandidateConfiguration(
                    topology="baseline-vllm",
                    decode_tp=1,
                    decode_replicas=1,
                ),
            )
        )
        store.save(
            _record(
                f"configured-{index}",
                CandidateConfiguration(
                    topology="baseline-vllm",
                    decode_tp=1,
                    decode_replicas=1,
                    prefill_tp=1,
                    prefill_replicas=1,
                ),
            )
        )
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

    seeds = HistoricalCandidateSeedRetriever(store).retrieve("Qwen/Qwen3-8B", facts)

    assert seeds[0].proposal.provider_ref == "baseline-vllm"
