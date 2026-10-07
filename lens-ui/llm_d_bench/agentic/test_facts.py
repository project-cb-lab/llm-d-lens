"""Tests for Agentic planning evidence resolution."""

import asyncio
from types import SimpleNamespace

from llm_d_bench.agentic.facts import _configuration_topology, resolve_planning_facts
from llm_d_bench.agentic.planner import PlanningFacts, WorkloadProfile


def test_resolver_uses_live_cluster_capacity_and_free_memory(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {
            "hardware": {
                "gpuCount": 4,
                "availableGpuCount": 3,
                "vramBytes": 4 * 32 * 1024**3,
                "memoryBytes": 100 * 1024**3,
                "memoryUsagePercent": 25,
            },
        }

    monkeypatch.setattr("llm_d_bench.agentic.facts.build_overview", overview)

    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=1, free_gpu_count=99, cpu_buffer_gib=1),
            include_supplementary_evidence=False,
        )
    )

    assert resolved.facts.free_gpu_count == 3
    assert resolved.facts.vram_per_gpu_gib == 32
    assert resolved.facts.cpu_buffer_gib == 75
    assert resolved.facts.evidence_ids == ("cluster-overview:cluster-a",)


def test_resolver_preserves_operator_preference_for_deterministic_fallback(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 1, "availableGpuCount": 1, "vramBytes": 32 * 1024**3}}

    monkeypatch.setattr("llm_d_bench.agentic.facts.build_overview", overview)
    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                operator_preference="prefer prefix cache",
            ),
            include_supplementary_evidence=False,
        )
    )

    assert resolved.facts.operator_preference == "prefer prefix cache"


def test_resolver_applies_requested_total_or_custom_gpu_capacity(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 8, "availableGpuCount": 3, "vramBytes": 8 * 32 * 1024**3}}

    monkeypatch.setattr("llm_d_bench.agentic.facts.build_overview", overview)
    all_capacity = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                hardware_capacity_mode="all",
            ),
            include_supplementary_evidence=False,
        )
    )
    custom_capacity = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                hardware_capacity_mode="custom",
                custom_gpu_count=5,
            ),
            include_supplementary_evidence=False,
        )
    )

    assert all_capacity.facts.free_gpu_count == 8
    assert custom_capacity.facts.free_gpu_count == 5
    assert custom_capacity.evidence[0].details["capacity_mode"] == "custom"


def test_resolver_retains_hard_cluster_facts_when_supplementary_sources_fail(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 1, "availableGpuCount": 1, "vramBytes": 32 * 1024**3}}

    async def unavailable_aic(_request):
        from llm_d_bench.aic.service import AICError

        raise AICError("offline")

    async def unavailable_simulation(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("llm_d_bench.agentic.facts.build_overview", overview)
    monkeypatch.setattr("llm_d_bench.agentic.facts.check_support", unavailable_aic)
    monkeypatch.setattr("llm_d_bench.agentic.facts.list_tasks", unavailable_simulation)

    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1),
        )
    )

    assert resolved.facts.free_gpu_count == 1
    assert {item.source for item in resolved.evidence if item.status == "unavailable"} >= {"aic", "simulation"}


def test_resolver_uses_measured_workload_for_aic_and_normalizes_predictions(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts.registry.require_cluster", lambda cluster_id: SimpleNamespace(id=cluster_id)
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 4, "availableGpuCount": 4, "vramBytes": 4 * 32 * 1024**3}}

    captured = {}

    async def support(_request):
        return SimpleNamespace(supported=True, model_dump=lambda: {"supported": True})

    async def aic_search(request):
        captured["request"] = request
        return SimpleNamespace(
            model_dump=lambda: {
                "configs": [
                    {
                        "mode": "agg",
                        "tp": 1,
                        "replicas": 1,
                        "ttft_ms": 300,
                        "tpot_ms": 25,
                        "throughput_tokens_per_sec": 700,
                    }
                ]
            }
        )

    monkeypatch.setattr("llm_d_bench.agentic.facts.build_overview", overview)
    monkeypatch.setattr("llm_d_bench.agentic.facts.check_support", support)
    monkeypatch.setattr("llm_d_bench.agentic.facts.search", aic_search)
    monkeypatch.setattr("llm_d_bench.agentic.facts._historical_topology_by_execution", lambda: {})
    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                workload_profile=WorkloadProfile(mean_input_tokens=4096, mean_output_tokens=512),
            ),
        )
    )

    assert captured["request"].mean_input_tokens == 4096
    assert captured["request"].mean_output_tokens == 512
    assert resolved.trace[-1]["request"]["mean_input_tokens"] == 4096
    assert resolved.trace[-1]["request"]["mean_output_tokens"] == 512
    assert resolved.trace[-1]["search_status"] == "available"
    assert resolved.facts.aic_predictions[0].ttft_ms == 300


def test_resolver_persists_similar_completed_history_as_benchmark_evidence(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts.registry.require_cluster", lambda cluster_id: SimpleNamespace(id=cluster_id)
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 4, "availableGpuCount": 4, "vramBytes": 4 * 32 * 1024**3}}

    async def unavailable_aic(_request):
        from llm_d_bench.aic.service import AICError

        raise AICError("offline")

    captured = {}

    async def history(**kwargs):
        captured.update(kwargs)
        request = SimpleNamespace(successful=True, ttft_ms=310, tpot_ms=24, input_tokens=4096, output_tokens=512)
        result = SimpleNamespace(summary={"throughput_tps": 720}, per_request=[request])
        task = SimpleNamespace(
            id="abcdef12", scenario="chat", result=result, endpoint_deployment_execution_id="execution-1"
        )
        return SimpleNamespace(total=1, tasks=[task])

    monkeypatch.setattr("llm_d_bench.agentic.facts.build_overview", overview)
    monkeypatch.setattr("llm_d_bench.agentic.facts.check_support", unavailable_aic)
    monkeypatch.setattr("llm_d_bench.agentic.facts.list_tasks", history)
    monkeypatch.setattr(
        "llm_d_bench.agentic.facts._historical_topology_by_execution",
        lambda: {
            "execution-1": {
                "provider_ref": "pd-disaggregation",
                "replicas": 1,
                "tensor_parallel_size": 1,
                "prefill_replicas": 1,
                "prefill_tensor_parallel_size": 1,
                "guide_variant": "vllm",
                "max_model_len": 4096,
                "gpu_memory_utilization": 0.9,
                "required_gpus": 2,
            },
        },
    )

    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                workload_profile=WorkloadProfile(mean_input_tokens=4096, mean_output_tokens=512),
            ),
        )
    )

    assert captured == {"status": "completed", "model": "Qwen/Qwen3-8B", "limit": 20}
    simulation_evidence = next(item for item in resolved.evidence if item.id == "simulation-history")
    assert simulation_evidence.details["total"] == 1
    assert simulation_evidence.details["benchmark_record_count"] >= 1
    assert "observations" not in simulation_evidence.details


def test_configuration_topology_captures_candidate_equivalent_pd_fields():
    configuration = SimpleNamespace(
        provider_ref="pd-disaggregation",
        provenance={"agentic_candidate_id": "legacy-internal-id"},
        content={
            "decode": {"replicaCount": 2, "tensorParallelSize": 2, "maxModelLen": 8192, "gpuMemoryUtilization": 0.9},
            "prefill": {"replicaCount": 1, "tensorParallelSize": 2},
            "guideVariant": "vllm",
        },
    )

    topology = _configuration_topology(configuration)

    assert topology == {
        "candidate_id": "legacy-internal-id",
        "provider_ref": "pd-disaggregation",
        "replicas": 2,
        "tensor_parallel_size": 2,
        "prefill_replicas": 1,
        "prefill_tensor_parallel_size": 2,
        "guide_variant": "vllm",
        "max_model_len": 8192,
        "gpu_memory_utilization": 0.9,
        "required_gpus": 6,
    }
