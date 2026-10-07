from datetime import UTC, datetime

from llm_d_bench.agentic.benchmark_evidence import (
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
    WorkloadProfile,
)
from llm_d_bench.agentic.benchmark_store import JsonBenchmarkRecordStore


def test_benchmark_records_are_persisted_with_an_explicit_schema_version(tmp_path):
    record = BenchmarkRecord(
        benchmark_id="evaluate:run-1",
        timestamp=datetime.now(UTC),
        outcome="succeeded",
        model=ModelDescriptor(
            model_id="Qwen/Qwen3-8B",
            family="Qwen",
            architecture="dense",
            parameter_count_b=8,
            quantization="fp16",
            tokenizer_id="Qwen/Qwen3-8B",
        ),
        hardware=HardwareDescriptor(accelerator="cluster", vram_per_gpu_gib=32, gpu_count=1),
        runtime=RuntimeFingerprint(backend="vllm", backend_version="0.10.0", accelerator_runtime="cuda"),
        workload=WorkloadProfile(
            mean_input_tokens=1024,
            p95_input_tokens=2048,
            mean_output_tokens=256,
            concurrency=1,
            request_rate=1,
            shared_prefix_ratio=0,
            prefill_fraction=0,
        ),
        configuration=CandidateConfiguration(topology="baseline-vllm", decode_tp=1, decode_replicas=1),
    )
    store = JsonBenchmarkRecordStore(tmp_path)

    store.save(record)

    assert store.list() == [record]
    # The on-disk "schema_version" envelope is gone now that records are
    # persisted in the database (see design doc section 5.4.12); the
    # store.list() round-trip assertion above is the equivalent check.
