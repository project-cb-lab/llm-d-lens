"""Tests for deterministic, bounded Agentic configuration planning."""

import asyncio
import json

import pytest

from llm_d_bench.agentic.planner import (
    AICCandidatePrediction,
    DeterministicPlanner,
    OpenAICompatiblePlanner,
    OpenAIPlannerError,
    PerformanceScoreInput,
    PlannedCandidate,
    PlanningFacts,
    WeightedBenchmarkSample,
    WorkloadProfile,
    minimum_tensor_parallelism,
)


@pytest.fixture(autouse=True)
def mock_provider_validation(monkeypatch):
    async def validate(_url):
        pass

    monkeypatch.setattr("llm_d_bench.ai_providers.service.validate_provider_endpoint", validate)


def test_planner_selects_the_smallest_feasible_standard_vllm_topology():
    facts = PlanningFacts(model_weight_gib=20, vram_per_gpu_gib=32, free_gpu_count=4)
    planner = DeterministicPlanner()

    decision = planner.decide(facts, planner.candidates(facts))

    assert minimum_tensor_parallelism(facts) == 1
    assert decision.action == "select_candidate"
    assert decision.candidate_id == "optimized-baseline-tp1-r1"


def test_planner_maps_exact_aggregated_aic_predictions_to_optimized_baseline():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        aic_predictions=(
            AICCandidatePrediction(
                mode="agg",
                tensor_parallel_size=1,
                replicas=1,
                ttft_ms=100,
                tpot_ms=10,
                throughput_tokens_per_sec=1000,
            ),
        ),
    )
    candidate = next(
        candidate
        for candidate in DeterministicPlanner().candidates(facts)
        if candidate.provider_ref == "optimized-baseline"
        and candidate.tensor_parallel_size == 1
        and candidate.replicas == 1
    )

    prediction = DeterministicPlanner._aic_prediction(facts, candidate)

    assert prediction is not None
    assert prediction.throughput_tokens_per_sec == 1000


def test_balanced_policy_selects_the_smallest_resource_candidate_when_extra_capacity_is_not_proportional():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, throughput_tokens_per_sec=100),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=2, replicas=1, throughput_tokens_per_sec=140),
        ),
    )
    planner = DeterministicPlanner()

    baseline_candidates = [
        candidate for candidate in planner.candidates(facts) if candidate.provider_ref == "baseline-vllm"
    ]

    assert planner.decide(facts, baseline_candidates).candidate_id == "baseline-vllm-tp1-r1"


def test_balanced_goal_only_uses_more_resources_for_proportional_predicted_gain():
    planner = DeterministicPlanner()
    common = {
        "model_weight_gib": 8,
        "vram_per_gpu_gib": 32,
        "free_gpu_count": 4,
    }
    insufficient_gain = PlanningFacts(
        **common,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, throughput_tokens_per_sec=100),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=2, replicas=1, throughput_tokens_per_sec=140),
        ),
    )
    proportional_gain = PlanningFacts(
        **common,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, throughput_tokens_per_sec=100),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=2, replicas=1, throughput_tokens_per_sec=300),
        ),
    )

    insufficient_candidates = [
        candidate for candidate in planner.candidates(insufficient_gain) if candidate.provider_ref == "baseline-vllm"
    ]
    proportional_candidates = [
        candidate for candidate in planner.candidates(proportional_gain) if candidate.provider_ref == "baseline-vllm"
    ]

    assert planner.decide(insufficient_gain, insufficient_candidates).candidate_id == "baseline-vllm-tp1-r1"
    assert planner.decide(proportional_gain, proportional_candidates).candidate_id == "baseline-vllm-tp2-r1"


def test_prefill_heavy_workload_prioritizes_pd_over_default_minimum_gpu_policy():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        prefill_heavy=True,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, throughput_tokens_per_sec=100),
            AICCandidatePrediction(
                mode="disagg",
                tensor_parallel_size=1,
                replicas=1,
                prefill_tensor_parallel_size=1,
                prefill_replicas=1,
                throughput_tokens_per_sec=140,
            ),
        ),
    )
    planner = DeterministicPlanner()

    decision = planner.decide(facts, planner.candidates(facts))

    assert decision.candidate_id == "pd-disaggregation-p1-d1-tp1"


@pytest.mark.parametrize("operator_preference", ["", "use less GPU resources"])
def test_balanced_and_economic_policies_prefer_lower_tp_before_aic_metrics_for_equal_gpu_topologies(
    operator_preference,
):
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        operator_preference=operator_preference,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=2, throughput_tokens_per_sec=100),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=2, replicas=1, throughput_tokens_per_sec=100),
        ),
    )
    planner = DeterministicPlanner()
    candidates = [
        candidate
        for candidate in planner.candidates(facts)
        if candidate.provider_ref == "baseline-vllm" and candidate.required_gpus == 2
    ]

    assert planner.decide(facts, candidates).candidate_id == "baseline-vllm-tp1-r2"


def test_planner_rejects_topologies_when_no_free_cards_meet_the_memory_bound():
    facts = PlanningFacts(model_weight_gib=80, vram_per_gpu_gib=32, free_gpu_count=1, context_length=8192)
    planner = DeterministicPlanner()

    candidates = planner.candidates(facts)
    decision = planner.decide(facts, candidates)

    assert minimum_tensor_parallelism(facts) == 4
    assert decision.action == "cannot_satisfy"
    assert "insufficient free accelerator cards" in decision.unmet_constraints
    assert planner.score(facts, candidates) == []


def test_planner_disables_only_tiered_prefix_cache_when_cpu_buffer_is_insufficient():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        cpu_buffer_gib=4,
        required_cpu_buffer_gib=8,
    )
    planner = DeterministicPlanner()

    candidates = planner.candidates(facts)
    decision = planner.decide(facts, candidates)

    assert decision.action == "select_candidate"
    assert {candidate.provider_ref for candidate in candidates if candidate.deployable} >= {
        "baseline-vllm",
        "optimized-baseline",
        "precise-prefix-cache-routing",
        "pd-disaggregation",
    }
    assert not any(candidate.provider_ref == "tiered-prefix-cache" for candidate in candidates)


def test_planner_generates_registered_guides_independently_of_workload_shape():
    planner = DeterministicPlanner()

    general = planner.candidates(PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=8))
    prefill_heavy = planner.candidates(
        PlanningFacts(
            model_weight_gib=8,
            vram_per_gpu_gib=32,
            free_gpu_count=8,
            prefill_heavy=True,
        )
    )

    assert {candidate.provider_ref for candidate in general} >= {
        "baseline-vllm",
        "optimized-baseline",
        "tiered-prefix-cache",
        "precise-prefix-cache-routing",
    }
    assert any(candidate.provider_ref == "pd-disaggregation" for candidate in prefill_heavy)
    decision = planner.decide(
        PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=8, shared_prefix_ratio=0.8),
        planner.candidates(
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=32,
                free_gpu_count=8,
                shared_prefix_ratio=0.8,
            )
        ),
    )
    assert decision.candidate_id is not None
    assert decision.candidate_id.startswith("tiered-prefix-cache")


def test_operator_preference_can_prioritize_cache_routing_candidates():
    planner = DeterministicPlanner()
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=8,
        operator_preference="Prefer prefix cache routing for this deployment.",
    )

    decision = planner.decide(facts, planner.candidates(facts))

    assert decision.candidate_id is not None
    assert decision.candidate_id.startswith("tiered-prefix-cache")


def test_code_generation_operator_preference_prioritizes_prefix_cache_over_aic_baseline():
    planner = DeterministicPlanner()
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        operator_preference="use case: code-generation",
        aic_predictions=(
            AICCandidatePrediction(
                mode="agg",
                tensor_parallel_size=1,
                replicas=1,
                throughput_tokens_per_sec=700,
            ),
        ),
    )

    decision = planner.decide(facts, planner.candidates(facts))

    assert decision.candidate_id is not None
    assert decision.candidate_id.startswith("tiered-prefix-cache")


def test_planner_prioritizes_tiered_prefix_cache_for_offloading_preference_without_aic():
    planner = DeterministicPlanner()
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=8,
        ttft_slo_ms=500,
        tpot_slo_ms=50,
        operator_preference="more like offloading",
    )

    decision = planner.decide(facts, planner.candidates(facts))

    assert decision.candidate_id is not None
    assert decision.candidate_id.startswith("tiered-prefix-cache")


def test_planner_maps_resource_preference_to_economic_and_performance_tendencies():
    planner = DeterministicPlanner()
    common = {"model_weight_gib": 8, "vram_per_gpu_gib": 32, "free_gpu_count": 2}
    candidates = planner.candidates(PlanningFacts(**common))

    economic = planner.score(PlanningFacts(**common, operator_preference="use less GPU resources"), candidates)
    performance = planner.score(PlanningFacts(**common, operator_preference="more GPU for performance"), candidates)

    assert economic[0].required_gpus == 1
    assert performance[0].required_gpus == 2


def test_planner_combines_more_gpu_and_distributed_preferences():
    planner = DeterministicPlanner()
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        operator_preference="more GPU and distributed",
    )

    decision = planner.decide(facts, planner.candidates(facts))

    assert decision.candidate_id is not None
    assert decision.candidate_id.startswith("pd-disaggregation")
    assert (
        next(
            candidate for candidate in planner.candidates(facts) if candidate.id == decision.candidate_id
        ).required_gpus
        == 4
    )


def test_planner_derives_heuristics_from_measured_workload_profile():
    planner = DeterministicPlanner()
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=8,
        workload_profile=WorkloadProfile(
            mean_input_tokens=4096,
            p95_input_tokens=8192,
            mean_output_tokens=512,
            concurrency=16,
            request_rate=4,
            shared_prefix_ratio=0.8,
            prefill_p95_ms=80,
            decode_p95_ms=20,
        ),
    )

    candidates = planner.candidates(facts)

    assert {candidate.provider_ref for candidate in candidates} >= {
        "tiered-prefix-cache",
        "precise-prefix-cache-routing",
    }
    assert planner.decide(facts, candidates).candidate_id.startswith("tiered-prefix-cache")


def test_planner_uses_case_defaults_when_workload_profile_is_missing_or_empty():
    planner = DeterministicPlanner()
    common = {"model_weight_gib": 8, "vram_per_gpu_gib": 32, "free_gpu_count": 8}

    code_candidates = planner.candidates(PlanningFacts(use_case="code-generation", **common))
    long_input_candidates = planner.candidates(
        PlanningFacts(
            use_case="long-inputs",
            workload_profile=WorkloadProfile(),
            **common,
        )
    )

    assert planner.decide(PlanningFacts(use_case="code-generation", **common), code_candidates).candidate_id.startswith(
        "tiered-prefix-cache"
    )
    assert planner.decide(
        PlanningFacts(use_case="long-inputs", **common), long_input_candidates
    ).candidate_id.startswith("pd-disaggregation")
    assert {candidate.provider_ref for candidate in code_candidates} == {
        candidate.provider_ref for candidate in planner.candidates(PlanningFacts(use_case="general-chat", **common))
    }


def test_planner_rejects_incomplete_measured_prefill_profile():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=8,
        workload_profile=WorkloadProfile(prefill_p95_ms=80),
    )

    with pytest.raises(ValueError, match="provided together"):
        DeterministicPlanner().candidates(facts)


def test_planner_prefers_exact_aic_prediction_that_meets_slos():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        ttft_slo_ms=500,
        tpot_slo_ms=50,
        aic_predictions=(
            AICCandidatePrediction(
                mode="agg",
                tensor_parallel_size=1,
                replicas=1,
                ttft_ms=300,
                tpot_ms=25,
                throughput_tokens_per_sec=700,
            ),
        ),
    )
    planner = DeterministicPlanner()

    applicable_candidates = [
        candidate for candidate in planner.candidates(facts) if candidate.provider_ref == "baseline-vllm"
    ]
    decision = planner.decide(facts, applicable_candidates)

    assert decision.candidate_id == "baseline-vllm-tp1-r1"
    assert "AIC-predicted" in decision.rationale
    ranking = planner.ranking_trace(facts, applicable_candidates)
    assert ranking[0]["candidate_id"] == decision.candidate_id
    assert ranking[0]["aic_prediction"]["ttft_ms"] == 300
    assert ranking[0]["slo_status"] == "satisfied"


def test_planner_does_not_demote_aic_unsupported_guide_without_latency_slos():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        use_case="code-generation",
        aic_predictions=(
            AICCandidatePrediction(
                mode="agg",
                tensor_parallel_size=1,
                replicas=1,
                ttft_ms=300,
                tpot_ms=25,
                throughput_tokens_per_sec=700,
            ),
        ),
    )
    planner = DeterministicPlanner()

    decision = planner.decide(facts, planner.candidates(facts))

    assert decision.candidate_id == "tiered-prefix-cache-tp1-r1"
    assert decision.rationale == "Selected the smallest topology that passes VRAM, free-card, and CPU-buffer checks."


def test_planner_ranks_aic_slo_failure_after_candidates_without_prediction():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        ttft_slo_ms=500,
        aic_predictions=(AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, ttft_ms=600),),
    )
    planner = DeterministicPlanner()

    scored = planner.score(facts, planner.candidates(facts))

    assert scored.index(next(candidate for candidate in scored if candidate.id == "baseline-vllm-tp1-r1")) > 0


def test_planner_prefers_closer_unmet_slo_over_smaller_topology():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=8,
        ttft_slo_ms=30,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, ttft_ms=44.872),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=8, replicas=1, ttft_ms=42.592),
        ),
    )
    planner = DeterministicPlanner()
    minimum = next(candidate for candidate in planner.candidates(facts) if candidate.id == "baseline-vllm-tp1-r1")
    anchor = minimum.model_copy(
        update={
            "id": "baseline-vllm-tp8-r1",
            "tensor_parallel_size": 8,
            "required_gpus": 8,
        }
    )

    decision = planner.decide(facts, [minimum, anchor])

    assert decision.candidate_id == anchor.id
    assert decision.slo_status == "not_satisfied"
    assert "remain unmet" in decision.rationale
    assert planner.score(facts, [minimum, anchor])[0].id == anchor.id


def test_ai_planner_rejects_worse_known_slo_miss_even_when_closer_candidate_is_omitted():
    from llm_d_bench.agentic.planner import OpenAIPlannerRecommendation

    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=8,
        ttft_slo_ms=30,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, ttft_ms=44),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=8, replicas=1, ttft_ms=42),
        ),
    )
    minimum = next(
        candidate for candidate in DeterministicPlanner().candidates(facts) if candidate.id == "baseline-vllm-tp1-r1"
    )
    anchor = minimum.model_copy(
        update={
            "id": "baseline-vllm-tp8-r1",
            "tensor_parallel_size": 8,
            "required_gpus": 8,
        }
    )
    wrong = OpenAIPlannerRecommendation(
        candidate_id=minimum.id,
        candidate_ids=[minimum.id],
        scores={minimum.id: 0.9},
        confidence=0.8,
        rationale="Minimum topology.",
    )
    right = OpenAIPlannerRecommendation(
        candidate_id=anchor.id,
        candidate_ids=[anchor.id, minimum.id],
        scores={anchor.id: 0.9, minimum.id: 0.8},
        confidence=0.8,
        rationale="Closer to target.",
    )

    with pytest.raises(OpenAIPlannerError, match="larger known latency SLO miss"):
        OpenAICompatiblePlanner._validate_unmet_slo_ranking(facts, [minimum, anchor], wrong)
    OpenAICompatiblePlanner._validate_unmet_slo_ranking(facts, [minimum, anchor], right)


def test_planner_uses_measured_benchmark_evidence_for_slo_and_ranking():
    planner = DeterministicPlanner()
    candidate_id = "optimized-baseline-tp1-r1"
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        ttft_slo_ms=500,
        tpot_slo_ms=50,
        performance_score_inputs=(
            PerformanceScoreInput(
                candidate_id=candidate_id,
                source="benchmark",
                status="measured",
                ttft_p95_ms=300,
                tpot_p95_ms=25,
                throughput_tokens_per_s=700,
                error_rate=0.01,
                confidence=0.9,
                effective_sample_size=4,
                evidence_refs=("benchmark:bench-1",),
            ),
        ),
    )

    candidates = planner.candidates(facts)
    decision = planner.decide(facts, candidates)

    assert decision.candidate_id == candidate_id
    assert decision.slo_status == "satisfied"
    assert decision.slo_confidence == 0.9
    assert (
        "benchmark:bench-1" in next(candidate for candidate in candidates if candidate.id == candidate_id).evidence_ids
    )


def test_planner_prioritizes_high_confidence_history_over_exact_aic_prediction():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        ttft_slo_ms=500,
        aic_predictions=(
            AICCandidatePrediction(
                mode="agg",
                tensor_parallel_size=1,
                replicas=1,
                ttft_ms=200,
                tpot_ms=20,
                throughput_tokens_per_sec=900,
            ),
        ),
        performance_score_inputs=(
            PerformanceScoreInput(
                candidate_id="optimized-baseline-tp1-r1",
                source="benchmark",
                status="measured",
                ttft_p95_ms=300,
                tpot_p95_ms=25,
                throughput_tokens_per_s=700,
                confidence=0.9,
                effective_sample_size=4,
            ),
        ),
    )

    scored = DeterministicPlanner().score(facts, DeterministicPlanner().candidates(facts))

    assert scored[0].id == "optimized-baseline-tp1-r1"


def test_planner_blocks_candidate_with_strong_negative_benchmark_evidence():
    planner = DeterministicPlanner()
    candidate_id = "optimized-baseline-tp1-r1"
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=1,
        performance_score_inputs=(
            PerformanceScoreInput(
                candidate_id=candidate_id,
                source="benchmark",
                status="interpolated",
                confidence=0.5,
                effective_sample_size=3,
                has_blocking_negative_evidence=True,
                evidence_refs=("benchmark:oom-1",),
            ),
        ),
    )

    candidates = planner.candidates(facts)

    blocked = next(candidate for candidate in candidates if candidate.id == candidate_id)
    assert not blocked.deployable
    assert "blocking benchmark failure evidence" in blocked.rejection_reasons


@pytest.mark.parametrize(
    ("status", "confidence", "effective_sample_size", "blocking", "expected"),
    [
        ("measured", 0.5, 1, False, True),
        ("measured", 0.49, 4, False, False),
        ("interpolated", 0.5, 3, False, True),
        ("interpolated", 0.5, 2.99, False, False),
        ("estimated", 1, 10, False, False),
        ("unknown", 1, 10, False, False),
        ("measured", 1, 10, True, False),
    ],
)
def test_deterministic_planner_only_uses_evidence_that_meets_policy_thresholds(
    status,
    confidence,
    effective_sample_size,
    blocking,
    expected,
):
    evidence = PerformanceScoreInput(
        candidate_id="baseline-vllm-tp1-r1",
        source="benchmark",
        status=status,
        confidence=confidence,
        effective_sample_size=effective_sample_size,
        has_blocking_negative_evidence=blocking,
    )

    assert DeterministicPlanner._usable_performance_evidence(evidence) is expected


def test_deterministic_score_uses_evidence_slo_then_confidence_and_preserves_unknowns():
    planner = DeterministicPlanner()
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        ttft_slo_ms=500,
        tpot_slo_ms=50,
        performance_score_inputs=(
            PerformanceScoreInput(
                candidate_id="baseline-vllm-tp1-r1",
                source="benchmark",
                status="measured",
                ttft_p95_ms=550,
                tpot_p95_ms=20,
                confidence=0.95,
                effective_sample_size=5,
            ),
            PerformanceScoreInput(
                candidate_id="optimized-baseline-tp1-r1",
                source="benchmark",
                status="interpolated",
                ttft_p95_ms=400,
                tpot_p95_ms=30,
                confidence=0.5,
                effective_sample_size=3,
            ),
            PerformanceScoreInput(
                candidate_id="tiered-prefix-cache-tp1-r1",
                source="benchmark",
                status="estimated",
                ttft_p95_ms=200,
                confidence=1,
                effective_sample_size=10,
            ),
            PerformanceScoreInput(
                candidate_id="precise-prefix-cache-routing-tp1-r1",
                source="benchmark",
                status="measured",
                ttft_p95_ms=200,
                confidence=0.49,
                effective_sample_size=10,
            ),
        ),
    )
    candidates = planner.candidates(facts)
    scored = planner.score(facts, candidates)
    scored_by_id = {candidate.id: candidate for candidate in scored}

    assert scored[0].id == "optimized-baseline-tp1-r1"
    assert scored_by_id["optimized-baseline-tp1-r1"].slo_status == "satisfied"
    assert scored_by_id["optimized-baseline-tp1-r1"].slo_confidence == 0.5
    assert scored_by_id["baseline-vllm-tp1-r1"].slo_status == "not_satisfied"
    assert scored_by_id["baseline-vllm-tp1-r1"].slo_confidence == 0.95
    assert scored_by_id["tiered-prefix-cache-tp1-r1"].slo_status == "unknown"
    assert scored_by_id["precise-prefix-cache-routing-tp1-r1"].slo_status == "unknown"


def test_openai_planner_receives_all_deployable_candidates_regardless_of_slo(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        ttft_slo_ms=500,
        performance_score_inputs=(
            PerformanceScoreInput(
                candidate_id="optimized-baseline-tp1-r1",
                source="benchmark",
                status="measured",
                ttft_p95_ms=300,
                confidence=0.9,
                effective_sample_size=4,
                evidence_refs=("benchmark:pass",),
                weighted_samples=(
                    WeightedBenchmarkSample(
                        benchmark_id="pass",
                        similarity=0.98,
                        time_decay=0.95,
                        runtime_similarity=1.0,
                        match_confidence=0.93,
                        observed_at="2026-09-20T00:00:00+00:00",
                        outcome="succeeded",
                        ttft_p95_ms=300,
                        topology="optimized-baseline",
                    ),
                ),
            ),
            PerformanceScoreInput(
                candidate_id="baseline-vllm-tp1-r1",
                source="benchmark",
                status="measured",
                ttft_p95_ms=600,
                confidence=0.9,
                effective_sample_size=4,
            ),
        ),
    )
    candidates = DeterministicPlanner().candidates(facts)
    captured: dict = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"optimized-baseline-tp1-r1",'
                                '"candidate_ids":["optimized-baseline-tp1-r1"],'
                                '"scores":{"optimized-baseline-tp1-r1":0.9},'
                                '"confidence":0.9,"rationale":"meets SLO"}'
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    decision, _ = asyncio.run(planner.recommend(facts, candidates))

    context, _separator, _use_case = captured["json"]["messages"][1]["content"].partition("\n\n")
    payload = json.loads(context)
    provider_candidate_ids = {item["id"] for item in payload["valid_candidates"]}
    deployable_candidate_ids = {candidate.id for candidate in candidates if candidate.deployable}
    assert len(provider_candidate_ids) == min(10, len(deployable_candidate_ids))
    assert provider_candidate_ids <= deployable_candidate_ids
    assert "performance_score_inputs" not in payload["planning_facts"]
    optimized_candidate = next(
        item for item in payload["valid_candidates"] if item["id"] == "optimized-baseline-tp1-r1"
    )
    assert "benchmark_evidence" not in optimized_candidate
    assert optimized_candidate["historical_benchmarks"] == [
        {
            "benchmark_id": "pass",
            "similarity": 0.98,
            "time_decay": 0.95,
            "runtime_similarity": 1.0,
            "match_confidence": 0.93,
            "observed_at": "2026-09-20T00:00:00+00:00",
            "outcome": "succeeded",
            "ttft_p95_ms": 300,
            "tpot_p95_ms": None,
            "throughput_tokens_per_s": None,
            "error_rate": None,
            "failure_reason": None,
            "topology": "optimized-baseline",
            "decode_tp": 1,
            "decode_replicas": 1,
            "prefill_tp": None,
            "prefill_replicas": None,
            "optimizations": [],
        }
    ]
    assert decision.slo_status == "satisfied"
    assert decision.slo_confidence == 0.9


def test_openai_compatible_planner_rejects_an_unvalidated_candidate(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1)
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"invented","candidate_ids":["baseline-vllm-tp1-r1"],"scores":{'
                                '"baseline-vllm-tp1-r1":0.9},'
                                '"confidence":0.9,"rationale":"invalid"}'
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(OpenAIPlannerError, match="invalid candidate"):
        asyncio.run(planner.recommend(facts, [candidate]))


def test_openai_compatible_planner_sends_all_validated_candidates_without_resource_policy(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=2,
        aic_predictions=(
            AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, throughput_tokens_per_sec=100),
            AICCandidatePrediction(mode="agg", tensor_parallel_size=2, replicas=1, throughput_tokens_per_sec=200),
        ),
    )
    candidates = DeterministicPlanner().candidates(facts)
    captured: dict = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"baseline-vllm-tp2-r1",'
                                '"candidate_ids":["baseline-vllm-tp2-r1","baseline-vllm-tp1-r1"],'
                                '"scores":{"baseline-vllm-tp2-r1":0.9,"baseline-vllm-tp1-r1":0.8},'
                                '"confidence":0.9,"rationale":"AIC-backed choice"}'
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    decision, _ = asyncio.run(
        planner.recommend(
            facts,
            [
                candidate
                for candidate in candidates
                if candidate.deployable and candidate.provider_ref == "baseline-vllm"
            ],
            operator_prompt="prioritize AIC recommendation",
        )
    )
    assert "name every other returned candidate ID" in captured["json"]["messages"][0]["content"]
    supplied = json.loads(captured["json"]["messages"][1]["content"].partition("\n\n")[0])["valid_candidates"]
    supplied_by_id = {candidate["id"]: candidate for candidate in supplied}
    assert decision.candidate_id == "baseline-vllm-tp2-r1"
    assert "not_applicable" in captured["json"]["messages"][0]["content"]
    assert "Return exactly 3 candidate ID(s)" in captured["json"]["messages"][0]["content"]
    assert "resource_policy" not in supplied_by_id["baseline-vllm-tp1-r1"]
    assert "resource_policy" not in supplied_by_id["baseline-vllm-tp2-r1"]
    assert supplied_by_id["baseline-vllm-tp1-r1"]["performance_estimate_applicability"] == "applicable"


def test_planner_with_model_config_populates_capacity_metrics_and_filters_invalid_tp():
    llama_cfg = {
        "architectures": ["LlamaForCausalLM"],
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "intermediate_size": 14336,
        "vocab_size": 128256,
        "max_position_embeddings": 8192,
        "torch_dtype": "bfloat16",
    }
    facts = PlanningFacts(
        model_name="meta-llama/Llama-3-8B",
        model_config_dict=llama_cfg,
        model_weight_gib=15.0,
        vram_per_gpu_gib=80.0,
        free_gpu_count=8,
        context_length=4096,
    )
    planner = DeterministicPlanner()
    candidates = planner.candidates(facts)

    # TP=1 candidates should be deployable and have positive allocatable KV cache
    tp1_cand = next(c for c in candidates if c.provider_ref == "baseline-vllm" and c.tensor_parallel_size == 1)
    assert tp1_cand.deployable is True
    assert tp1_cand.allocatable_kv_cache_gib is not None and tp1_cand.allocatable_kv_cache_gib > 40.0
    assert tp1_cand.per_request_kv_cache_gib is not None and tp1_cand.per_request_kv_cache_gib > 0.4
    assert tp1_cand.max_concurrent_requests is not None and tp1_cand.max_concurrent_requests > 10


def test_planner_with_model_config_rejects_oom_topology():
    llama_cfg = {
        "architectures": ["LlamaForCausalLM"],
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "intermediate_size": 14336,
        "vocab_size": 128256,
        "max_position_embeddings": 8192,
        "torch_dtype": "bfloat16",
    }
    # 70B model on 16GB GPU (TP=1)
    facts = PlanningFacts(
        model_name="meta-llama/Llama-3-70B",
        model_config_dict=llama_cfg,
        model_weight_gib=140.0,
        vram_per_gpu_gib=16.0,
        free_gpu_count=4,
        context_length=4096,
    )
    planner = DeterministicPlanner()
    candidates = planner.candidates(facts)

    tp1_cand = next(c for c in candidates if c.provider_ref == "baseline-vllm" and c.tensor_parallel_size == 1)
    assert tp1_cand.deployable is False
    assert any("insufficient GPU memory" in r for r in tp1_cand.rejection_reasons)


def test_openai_compatible_planner_emits_scoring_progress(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=1,
        aic_predictions=(
            AICCandidatePrediction(
                mode="agg",
                tensor_parallel_size=1,
                replicas=1,
                throughput_tokens_per_sec=100,
            ),
        ),
    )
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )
    events: list[dict] = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"baseline-vllm-tp1-r1",'
                                '"candidate_ids":["baseline-vllm-tp1-r1"],'
                                '"scores":{"baseline-vllm-tp1-r1":0.9},'
                                '"confidence":0.9,"rationale":"AIC-backed choice"}'
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    async def capture(event):
        events.append(event)

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    asyncio.run(planner.recommend(facts, [candidate], on_progress=capture))

    messages = [event["message"] for event in events]
    assert all(event["phase"] == "scoring" for event in events)
    assert (
        messages[0] == "Prepared 1 validated candidate(s) for AI scoring. AIC has exact estimates for 1 candidate(s)."
    )
    assert messages[1] == "Requesting AI ranking and structured scores (attempt 1 of 2)."
    assert messages[2].startswith("Validated AI scores for 1 candidate(s)")


def test_openai_candidate_context_marks_unsupported_guide_aic_as_not_applicable():
    candidate = PlannedCandidate(
        id="tiered-prefix-cache-tp1-r1",
        provider_ref="tiered-prefix-cache",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    context = OpenAICompatiblePlanner._candidate_context(
        PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1),
        candidate,
    )

    assert context["performance_estimate_applicability"] == "not_applicable"
    assert context["performance_estimate"] is None


def test_openai_compatible_planner_accepts_a_valid_candidate(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=2)
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"baseline-vllm-tp1-r1","candidate_ids":["baseline-vllm-tp1-r1"],"scores":{'
                                '"baseline-vllm-tp1-r1":0.8},'
                                '"confidence":0.8,"rationale":"valid"}'
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    decision, scored_candidates = asyncio.run(planner.recommend(facts, [candidate]))

    assert decision.candidate_id == candidate.id
    assert scored_candidates[0].score == 1.0


def test_openai_compatible_planner_extracts_final_valid_json_from_reasoning_text(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1)
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    class Client:
        async def complete(self, **_kwargs):
            return (
                'Draft {"candidate_id":"broken"} final answer: '
                '{"candidate_id":"baseline-vllm-tp1-r1",'
                '"candidate_ids":["baseline-vllm-tp1-r1"],'
                '"scores":{"baseline-vllm-tp1-r1":0.9},'
                '"confidence":0.8,"rationale":"Smallest valid topology."}'
            )

    monkeypatch.setattr("llm_d_bench.agentic.planner.client_for", lambda **_kwargs: Client())

    decision, scored = asyncio.run(planner.recommend(facts, [candidate]))

    assert decision.candidate_id == candidate.id
    assert scored[0].score == 1.0


def test_openai_compatible_planner_accepts_a_valid_preference_choice(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1)
    candidates = [
        PlannedCandidate(
            id="baseline-vllm-tp1-r1",
            provider_ref="baseline-vllm",
            replicas=1,
            tensor_parallel_size=1,
            max_model_len=4096,
            gpu_memory_utilization=0.9,
            required_gpus=1,
            deployable=True,
        ),
        PlannedCandidate(
            id="tiered-prefix-cache-tp1-r1",
            provider_ref="tiered-prefix-cache",
            replicas=1,
            tensor_parallel_size=1,
            max_model_len=4096,
            gpu_memory_utilization=0.9,
            required_gpus=1,
            deployable=True,
        ),
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"tiered-prefix-cache-tp1-r1",'
                                '"candidate_ids":["tiered-prefix-cache-tp1-r1","baseline-vllm-tp1-r1"],'
                                '"scores":{"tiered-prefix-cache-tp1-r1":0.9,"baseline-vllm-tp1-r1":0.8},'
                                '"confidence":0.9,"rationale":"preference"}'
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    decision, scored_candidates = asyncio.run(planner.recommend(facts, candidates))

    assert decision.candidate_id == "tiered-prefix-cache-tp1-r1"
    assert decision.confidence == 0.9
    assert scored_candidates[0].id == "tiered-prefix-cache-tp1-r1"
    assert scored_candidates[0].score == 1.0
    assert scored_candidates[1].score == pytest.approx(0.8 / 0.9)


def test_openai_compatible_planner_uses_anthropic_messages_api_when_configured(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner", "sk-ant-abc", provider_type="anthropic")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=2)
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )
    captured: dict = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            '{"candidate_id":"baseline-vllm-tp1-r1","candidate_ids":["baseline-vllm-tp1-r1"],"scores":{'
                            '"baseline-vllm-tp1-r1":0.8},'
                            '"confidence":0.8,"rationale":"valid"}'
                        ),
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, endpoint, *, json, headers):
            captured["endpoint"] = endpoint
            captured["json"] = json
            captured["headers"] = headers
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    decision, scored_candidates = asyncio.run(planner.recommend(facts, [candidate]))

    assert decision.candidate_id == candidate.id
    assert scored_candidates[0].score == 1.0
    assert captured["endpoint"] == "http://provider/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-abc"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in captured["headers"]
    assert "system" in captured["json"]
    assert "response_format" not in captured["json"]
    assert "Anthropic response contract" in captured["json"]["system"]
    assert "no markdown" in captured["json"]["system"]

    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=1,
        evidence_ids=("aic-1",),
        workload_profile=WorkloadProfile(mean_input_tokens=4096),
        aic_predictions=(AICCandidatePrediction(mode="agg", tensor_parallel_size=1, replicas=1, ttft_ms=300),),
    )
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )
    captured: dict = {}

    class OpenAIResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"candidate_id":"baseline-vllm-tp1-r1","candidate_ids":["baseline-vllm-tp1-r1"],"scores":{'
                                '"baseline-vllm-tp1-r1":0.8},'
                                '"confidence":0.8,"rationale":"valid"}'
                            )
                        }
                    }
                ]
            }

    class OpenAIClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return OpenAIResponse()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: OpenAIClient())

    decision, _ = asyncio.run(
        planner.recommend(
            facts,
            [candidate],
            operator_prompt="Prefer prefix cache routing when feasible.",
        )
    )

    assert captured["json"]["response_format"]["json_schema"]["strict"] is True
    system_prompt = captured["json"]["messages"][0]["content"]
    assert "Follow this priority order" in system_prompt
    assert "operator_preference must be the primary scoring criterion" in system_prompt
    assert "deployment use case and planning_facts workload signals" in system_prompt
    assert (
        "Do not let minimum GPU usage override a candidate that better fits significant workload signals"
        in system_prompt
    )
    assert "tie-breaker between candidates with comparable workload fit" in system_prompt
    assert "default when workload signals are absent or inconclusive" in system_prompt
    assert "code-generation or high shared_prefix_ratio" not in system_prompt
    assert "prefill_heavy workloads, rank pd-disaggregation" not in system_prompt
    assert "general-chat, rank optimized-baseline above baseline-vllm" not in system_prompt
    assert "Map offload to tiered-prefix-cache" not in system_prompt
    assert "disaggregation to pd-disaggregation" not in system_prompt
    assert "code-generation to prefix-cache providers" not in system_prompt
    assert "Prefer exact performance_estimate evidence" in system_prompt
    assert "Use the smallest suitable topology only as a tie-breaker" in system_prompt
    assert "explicitly prioritizes performance, throughput, or lower latency" in system_prompt
    assert "rank a feasible higher-replica candidate above the resource-preferred candidate" in system_prompt
    assert "GPU memory pressure" in system_prompt
    assert "baseline-vllm: direct standard vLLM deployment" in system_prompt
    assert "optimized-baseline: vLLM with llm-d prefix-affinity" in system_prompt
    assert "pd-disaggregation: separate prefill and decode roles" in system_prompt
    assert "tiered-prefix-cache: vLLM with tiered prefix caching that uses available CPU buffer" in system_prompt
    assert "to relieve GPU VRAM pressure" in system_prompt
    assert "historical_benchmarks are immutable measurements from past runs" in system_prompt
    assert "name every other returned candidate ID" in system_prompt
    assert "candidate-specific reason" in system_prompt
    assert "relative to the top candidate" in system_prompt
    assert "do not invent measurements or claim that an unknown estimate fails an SLO" in system_prompt
    assert "If only one candidate is returned" in system_prompt
    assert "unnecessary exact values" in system_prompt
    assert captured["json"]["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert (
        "uniqueItems" not in captured["json"]["response_format"]["json_schema"]["schema"]["properties"]["candidate_ids"]
    )
    assert captured["json"]["messages"][1]["content"]
    assert "Operator preference" in captured["json"]["messages"][1]["content"]
    assert "primary scoring criterion after hard constraints" in captured["json"]["messages"][1]["content"]
    assert "Prefer prefix cache routing when feasible." in captured["json"]["messages"][1]["content"]
    assert (
        "Deployment use case (important scenario-specific planning input):"
        in captured["json"]["messages"][1]["content"]
    )
    assert "general-chat" in captured["json"]["messages"][1]["content"]
    facts_content, _separator, _preference = captured["json"]["messages"][1]["content"].partition("\n\n")
    valid_candidate = json.loads(facts_content)["valid_candidates"][0]
    assert valid_candidate["id"] == candidate.id
    assert "resource_policy" not in valid_candidate
    assert valid_candidate["performance_estimate"] == {
        "ttft_ms": 300,
        "tpot_ms": None,
        "throughput_tokens_per_sec": None,
    }
    assert "vllm_arguments" not in valid_candidate
    assert decision.evidence_ids == ["aic-1"]


def test_openai_compatible_planner_retries_groq_structured_output_validation_failure(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1)
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )
    requests: list[dict] = []

    class Response:
        def __init__(self, payload, *, structured_failure=False):
            self.payload = payload
            self.structured_failure = structured_failure

        def raise_for_status(self):
            if self.structured_failure:
                response = __import__("httpx").Response(
                    400,
                    json={"error": {"param": "response_format", "code": "unsupported_uniqueItems"}},
                )
                raise __import__("httpx").HTTPStatusError(
                    "structured output failed",
                    request=__import__("httpx").Request("POST", "http://provider"),
                    response=response,
                )

        def json(self):
            return self.payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, _url, **kwargs):
            requests.append(kwargs["json"])
            if len(requests) == 1:
                return Response({}, structured_failure=True)
            return Response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"candidate_id":"baseline-vllm-tp1-r1","candidate_ids":["baseline-vllm-tp1-r1"],"scores":{'
                                    '"baseline-vllm-tp1-r1":0.8},"confidence":0.8,"rationale":"valid"}'
                                )
                            }
                        }
                    ]
                }
            )

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    decision, scored_candidates = asyncio.run(planner.recommend(facts, [candidate]))

    assert decision.candidate_id == candidate.id
    assert scored_candidates[0].score_source == "openai-compatible"
    assert requests[0]["response_format"]["type"] == "json_schema"
    # An unrecognized host demotes to the next rung ("json_object") rather
    # than dropping response_format entirely -- see `guess_structured_output_mode`.
    assert requests[1]["response_format"]["type"] == "json_object"
    assert requests[1]["max_tokens"] == 8192
    # The retry reuses the same system message, and appends a JSON-shape hint
    # to the user turn since json_object mode isn't schema-aware.
    assert requests[1]["messages"][0] == requests[0]["messages"][0]
    assert requests[1]["messages"][-1]["content"].startswith(requests[0]["messages"][-1]["content"])


def test_openai_compatible_planner_sends_every_deployable_candidate(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=8)
    candidates = DeterministicPlanner().candidates(facts)
    captured: dict = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            context, _separator, _use_case = captured["json"]["messages"][1]["content"].partition("\n\n")
            provider_candidates = json.loads(context)["valid_candidates"]
            candidate_id = provider_candidates[0]["id"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidate_id": candidate_id,
                                    "candidate_ids": [candidate_id],
                                    "scores": {candidate_id: 0.8},
                                    "confidence": 0.8,
                                    "rationale": "valid",
                                }
                            )
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    _decision, scored_candidates = asyncio.run(planner.recommend(facts, candidates))

    context, _separator, _use_case = captured["json"]["messages"][1]["content"].partition("\n\n")
    provider_candidates = json.loads(context)["valid_candidates"]
    assert len(provider_candidates) == 10
    assert len(provider_candidates) == min(10, len([candidate for candidate in candidates if candidate.deployable]))
    assert {candidate["provider_ref"] for candidate in provider_candidates} == {
        "baseline-vllm",
        "optimized-baseline",
        "pd-disaggregation",
        "tiered-prefix-cache",
        "precise-prefix-cache-routing",
    }
    assert len(scored_candidates) == 1


def test_openai_compatible_planner_rejects_missing_response_content(monkeypatch):
    planner = OpenAICompatiblePlanner("http://provider", "planner")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1)
    candidate = PlannedCandidate(
        id="baseline-vllm-tp1-r1",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=1,
        deployable=True,
    )

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": []}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(OpenAIPlannerError, match="no choices"):
        asyncio.run(planner.recommend(facts, [candidate]))


def test_from_provider_id_falls_back_to_environment_when_unset(monkeypatch):
    monkeypatch.setenv("AGENTIC_OPENAI_BASE_URL", "http://env-provider")
    monkeypatch.setenv("AGENTIC_OPENAI_MODEL", "env-model")

    planner = OpenAICompatiblePlanner.from_provider_id(None)

    assert planner is not None
    assert str(planner.settings.base_url) == "http://env-provider/"
    assert planner.settings.model == "env-model"


def test_from_provider_id_returns_none_when_unset_and_environment_unconfigured(monkeypatch):
    monkeypatch.delenv("AGENTIC_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AGENTIC_OPENAI_MODEL", raising=False)

    assert OpenAICompatiblePlanner.from_provider_id(None) is None


def test_from_provider_id_builds_planner_from_a_saved_provider(monkeypatch, tmp_path):
    from llm_d_bench.ai_providers import service as ai_providers_service
    from llm_d_bench.ai_providers.contracts import AIProviderCreateRequest
    from llm_d_bench.ai_providers.service import AIProviderService
    from llm_d_bench.ai_providers.store import AIProviderStore

    svc = AIProviderService(AIProviderStore(tmp_path))
    provider = svc.create(
        AIProviderCreateRequest(name="Saved", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-abc")
    )
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: svc)

    planner = OpenAICompatiblePlanner.from_provider_id(provider.id)

    assert planner is not None
    assert planner.settings.model == "gpt-4o-mini"
    assert planner.settings.provider_type == "openai"


def test_from_provider_id_carries_over_the_saved_providers_type(monkeypatch, tmp_path):
    from llm_d_bench.ai_providers import service as ai_providers_service
    from llm_d_bench.ai_providers.contracts import AIProviderCreateRequest
    from llm_d_bench.ai_providers.service import AIProviderService
    from llm_d_bench.ai_providers.store import AIProviderStore

    svc = AIProviderService(AIProviderStore(tmp_path))
    provider = svc.create(
        AIProviderCreateRequest(
            name="Saved Anthropic",
            baseUrl="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            apiKey="sk-ant-abc",
            providerType="anthropic",
        )
    )
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: svc)

    planner = OpenAICompatiblePlanner.from_provider_id(provider.id)

    assert planner is not None
    assert planner.settings.provider_type == "anthropic"


def test_from_provider_id_returns_none_when_the_provider_no_longer_exists(monkeypatch, tmp_path):
    from llm_d_bench.ai_providers import service as ai_providers_service
    from llm_d_bench.ai_providers.service import AIProviderService
    from llm_d_bench.ai_providers.store import AIProviderStore

    svc = AIProviderService(AIProviderStore(tmp_path))
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: svc)

    assert OpenAICompatiblePlanner.from_provider_id("missing-provider") is None
