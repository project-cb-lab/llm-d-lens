"""Tests for Agentic Deploy's delegation to existing Prism services."""

import asyncio
from dataclasses import replace
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llm_d_bench.agentic.facts import PlanningEvidence, ResolvedPlanningFacts
from llm_d_bench.agentic.generator import CandidateProposal
from llm_d_bench.agentic.historical_candidate_seeds import HistoricalCandidateSeed
from llm_d_bench.agentic.models import (
    AgenticCandidateRefinementRequest,
    AgenticDeploymentCreateRequest,
    AgenticDeploymentStatus,
)
from llm_d_bench.agentic.planner import (
    AICCandidatePrediction,
    DeterministicPlanner,
    OpenAICompatiblePlanner,
    OpenAIPlannerSettings,
    PerformanceScoreInput,
    PlanningFacts,
    WorkloadProfile,
)
from llm_d_bench.agentic.planning_snapshot import PlanningSnapshotStore
from llm_d_bench.agentic.service import AgenticDeploymentService

_ORIGINAL_VALIDATE_MODEL_CACHE = AgenticDeploymentService._validate_model_cache


async def _validate_model_cache(*_args, **_kwargs) -> None:
    pass


@pytest.fixture(autouse=True)
def validated_model_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(AgenticDeploymentService, "_validate_model_cache", staticmethod(_validate_model_cache))
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.PlanningSnapshotStore",
        lambda: PlanningSnapshotStore(tmp_path),
    )


def _request(**overrides) -> AgenticDeploymentCreateRequest:
    return AgenticDeploymentCreateRequest(
        model="Qwen/Qwen3-8B",
        deployment_name="agentic-qwen3",
        cluster_session_id="a" * 36,
        replicas=1,
        tensor_parallel_size=1,
        planning_facts={"model_weight_gib": 8, "vram_per_gpu_gib": 32, "free_gpu_count": 4},
        **overrides,
    )


def _resolved_facts(*, free_gpu_count=4, aic_predictions=()) -> ResolvedPlanningFacts:
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=free_gpu_count,
        aic_predictions=aic_predictions,
    )
    return ResolvedPlanningFacts(
        facts=facts,
        cluster_snapshot={"hardware": {"availableGpuCount": free_gpu_count}},
        evidence=[PlanningEvidence(id="cluster-overview:cluster-a", source="cluster-overview", status="available")],
    )


async def _resolve_facts(*_args, **_kwargs) -> ResolvedPlanningFacts:
    return _resolved_facts()


def test_create_generates_an_approval_plan_without_deploying(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment",
        lambda: None,
    )

    run = asyncio.run(service.create(_request()))

    assert run.status == AgenticDeploymentStatus.AWAITING_APPROVAL
    assert run.deployment_run_id is None
    assert run.configuration_artifact_id is None
    assert run.selected_candidate.replicas == 1
    assert run.selected_candidate.tensor_parallel_size == 1
    assert run.planning_evidence[0].source == "cluster-overview"
    assert run.decision_metadata is not None
    assert run.decision_metadata.planner == "deterministic"
    assert "Relative rank" in run.decision_metadata.score_method
    assert [item["phase"] for item in run.planning_trace] == [
        "input",
        "generation",
        "candidate_validation",
        "deterministic_ranking",
        "scoring",
        "selection",
    ]
    assert run.planning_trace[-1]["candidate_id"] == run.selected_candidate.id
    assert run.planning_trace[3]["ranking"][0]["candidate_id"] == run.selected_candidate.id


def test_create_writes_a_redacted_append_only_planning_snapshot(monkeypatch, tmp_path):
    service = AgenticDeploymentService(snapshot_store=PlanningSnapshotStore(tmp_path))
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    run = asyncio.run(service.create(_request(planner_prompt="do not persist this prompt")))

    assert run.planning_snapshot_id is not None
    assert run.planning_snapshot_path is not None
    payload = json.loads((tmp_path / run.planning_snapshot_path).read_text(encoding="utf-8"))
    assert payload["event"] == "created"
    assert payload["run_id"] == run.id
    assert payload["request"]["planner_prompt_present"] is True
    assert "planner_prompt" not in payload["request"]
    assert payload["selected_candidate"]["id"] == run.selected_candidate.id


def test_approve_publishes_configuration_then_starts_existing_deploy(monkeypatch):
    service = AgenticDeploymentService()
    session = SimpleNamespace(id="a" * 36, server_id="cluster-a")
    monkeypatch.setattr("llm_d_bench.agentic.service.require_active_session", lambda _session_id: session)
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    saved = {}

    async def start_run(request, **_kwargs):
        saved["configuration"] = request.configurations[0]
        saved["deployment"] = request
        return SimpleNamespace(id="deploy-run-1")

    monkeypatch.setattr("llm_d_bench.agentic.service.deployment_run_manager.start_run", start_run)
    run = asyncio.run(service.create(_request()))

    approved = asyncio.run(service.approve(run.id))

    assert approved.status == AgenticDeploymentStatus.DEPLOYING
    assert approved.configuration_artifact_id is None
    assert approved.deployment_run_id == "deploy-run-1"
    assert saved["configuration"].provenance["cluster_ref"]["session_id"] == session.id
    assert saved["deployment"].provenance["agentic_deployment_run_id"] == run.id


def test_select_candidate_only_allows_a_validated_alternative(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    run = asyncio.run(service.create(_request()))
    alternative = run.candidates[1]

    updated = service.select_candidate(run.id, alternative.id)

    assert updated.selected_candidate.id == alternative.id
    assert updated.decision is not None
    assert updated.decision.rationale.startswith("Selected by the user")
    with pytest.raises(ValueError, match="not a validated alternative"):
        service.select_candidate(run.id, "not-a-candidate")


def test_approve_carries_model_specific_vllm_arguments_to_deployment(monkeypatch):
    service = AgenticDeploymentService()
    session = SimpleNamespace(id="a" * 36, server_id="cluster-a")
    monkeypatch.setattr("llm_d_bench.agentic.service.require_active_session", lambda _session_id: session)
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    saved = {}

    async def start_run(request, **_kwargs):
        saved["configuration"] = request.configurations[0]
        return SimpleNamespace(id="deploy-run-1")

    monkeypatch.setattr("llm_d_bench.agentic.service.deployment_run_manager.start_run", start_run)
    run = asyncio.run(
        service.create(
            _request(
                vllm_arguments=[
                    {"name": "enable-auto-tool-choice", "value": "true"},
                    {"name": "tool-call-parser", "value": "hermes"},
                ]
            )
        )
    )

    asyncio.run(service.approve(run.id))

    arguments = saved["configuration"].content["customParameters"]
    assert {item["name"] for item in arguments} >= {"enable-auto-tool-choice", "tool-call-parser"}


def test_vllm_fields_are_removed_but_workload_signals_reach_candidate_planning(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    captured = {}

    async def resolve(_cluster_id, _model, facts, **_kwargs):
        captured["facts"] = facts
        return _resolved_facts()

    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", resolve)
    request = _request(
        max_model_len=32768,
        gpu_memory_utilization=0.6,
        max_num_seqs=8,
        max_num_batched_tokens=1024,
        vllm_arguments=[{"name": "enforce-eager", "value": "true"}],
    ).model_copy(
        update={
            "planning_facts": PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=32,
                free_gpu_count=4,
                context_length=32768,
                use_case="long-inputs",
                workload_profile=WorkloadProfile(mean_input_tokens=16384, concurrency=32),
                vllm_arguments=(("enforce-eager", "true"),),
            )
        }
    )
    asyncio.run(service.create(request))

    facts = captured["facts"]
    assert facts.context_length == 4096
    assert facts.use_case == "long-inputs"
    assert facts.workload_profile is not None
    assert facts.workload_profile.mean_input_tokens == 16384
    assert facts.vllm_arguments == ()


def test_automatic_execution_is_not_enabled(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )

    with pytest.raises(ValueError, match="automatic Agentic execution is not enabled"):
        asyncio.run(service.create(_request(execution_policy={"mode": "automatic"})))


def test_apply_mode_merges_only_validated_historical_candidate_seeds(monkeypatch):
    service = AgenticDeploymentService(historical_seed_mode="apply")
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=2)
    candidates = DeterministicPlanner().candidates(facts)
    monkeypatch.setattr(
        service._historical_seed_retriever,
        "retrieve",
        lambda *_args: (
            HistoricalCandidateSeed(
                proposal=CandidateProposal(
                    provider_ref="precise-prefix-cache-routing",
                    replicas=1,
                    tensor_parallel_size=1,
                    rationale="Qualified history.",
                ),
                evidence_refs=("benchmark:seed-1",),
            ),
            HistoricalCandidateSeed(
                proposal=CandidateProposal(
                    provider_ref="baseline-vllm",
                    replicas=3,
                    tensor_parallel_size=1,
                    rationale="Exceeds available capacity.",
                ),
                evidence_refs=("benchmark:invalid",),
            ),
        ),
    )

    merged, seed_count = asyncio.run(service._merge_historical_seeds("Qwen/Qwen3-8B", facts, candidates))

    seeded = next(candidate for candidate in merged if candidate.id == "precise-prefix-cache-routing-tp1-r1")
    assert seed_count == 1
    assert "benchmark:seed-1" in seeded.evidence_ids
    assert all(candidate.id != "baseline-vllm-tp1-r3" for candidate in merged)


def test_create_records_selected_benchmark_refs_in_decision_snapshot(monkeypatch, tmp_path):
    service = AgenticDeploymentService(snapshot_store=PlanningSnapshotStore(tmp_path))
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    def score_inputs(_self, _model, _facts, candidates):
        selected = DeterministicPlanner().decide(_facts, candidates).candidate_id
        return (
            PerformanceScoreInput(
                candidate_id=selected,
                source="benchmark",
                status="measured",
                evidence_refs=("benchmark:run-1", "benchmark:run-2"),
            ),
        )

    monkeypatch.setattr("llm_d_bench.agentic.service.BenchmarkPerformanceEvidence.score_inputs", score_inputs)
    run = asyncio.run(service.create(_request()))

    assert run.decision_evidence_ids.count("benchmark:run-1") == 1
    assert "benchmark:run-2" in run.decision_evidence_ids
    snapshot = json.loads((tmp_path / run.planning_snapshot_path).read_text(encoding="utf-8"))
    assert snapshot["decision_evidence_ids"] == run.decision_evidence_ids


def test_decision_evidence_includes_selected_ai_raw_samples_once():
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=2)
    candidate = (
        DeterministicPlanner()
        .candidates(facts)[0]
        .model_copy(
            update={
                "evidence_ids": ["benchmark:shared", "benchmark:seed"],
                "historical_benchmarks": [
                    {"benchmark_id": "shared"},
                    {"benchmark_id": "raw"},
                ],
            }
        )
    )

    assert AgenticDeploymentService._decision_evidence_ids(facts, candidate) == [
        "benchmark:shared",
        "benchmark:seed",
        "benchmark:raw",
    ]


def test_create_falls_back_to_deterministic_planning_when_provider_fails(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class FailingProvider:
        async def recommend(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment",
        lambda: FailingProvider(),
    )

    run = asyncio.run(service.create(_request()))

    assert run.planner == "deterministic"
    assert run.planner_fallback_reason is not None
    assert run.decision is not None
    assert run.decision.action == "select_candidate"


def test_create_falls_back_when_provider_selects_an_unvalidated_candidate(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class InvalidProvider:
        async def recommend(self, *_args, **_kwargs):
            from llm_d_bench.agentic.planner import PlannerDecision

            return PlannerDecision(
                action="select_candidate",
                candidate_id="external-invented-candidate",
                confidence=0.8,
                rationale="provider choice",
            ), []

    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment",
        lambda: InvalidProvider(),
    )

    run = asyncio.run(service.create(_request()))

    assert run.planner == "deterministic"
    assert run.planner_fallback_reason == "invalid_response"
    assert run.selected_candidate.id != "external-invented-candidate"
    assert run.selected_candidate.id in {candidate.id for candidate in run.candidates}


def test_create_prefers_openai_compatible_planning_when_provider_is_available(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class AvailableProvider:
        async def recommend(self, _facts, candidates, **_kwargs):
            selected = next(candidate for candidate in candidates if candidate.deployable)
            from llm_d_bench.agentic.planner import PlannerDecision

            decision = PlannerDecision(
                action="select_candidate",
                candidate_id=selected.id,
                confidence=0.8,
                rationale="provider choice",
            )
            scored = [
                candidate.model_copy(update={"score": 0.9, "score_source": "openai-compatible"})
                for candidate in candidates
                if candidate.deployable
            ]
            return decision, scored

    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment",
        lambda: AvailableProvider(),
    )

    run = asyncio.run(service.create(_request()))

    assert run.planner == "openai-compatible"
    assert run.decision is not None
    assert run.decision.rationale == "provider choice"
    assert run.decision_metadata is not None
    assert run.decision_metadata.planner == "openai-compatible"
    assert run.decision_metadata.planner_model is None
    assert 1 <= len(run.candidates) <= 3
    assert all(candidate.deployable and candidate.score_source == "openai-compatible" for candidate in run.candidates)


def test_create_shows_three_of_provider_selected_candidates(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class Provider:
        async def recommend(self, _facts, candidates, **_kwargs):
            selected = [
                candidate.model_copy(update={"score": 1 - index / 10, "score_source": "openai-compatible"})
                for index, candidate in enumerate(candidates[:10])
            ]
            from llm_d_bench.agentic.planner import PlannerDecision

            return PlannerDecision(
                action="select_candidate", candidate_id=selected[0].id, confidence=0.8, rationale="provider choice"
            ), selected

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment", lambda: Provider())
    run = asyncio.run(service.create(_request()))
    assert len(run.candidates) == 3


def test_create_uses_valid_ai_generated_candidates_without_falling_back(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )

    async def unexpected_resolve(*_args, **_kwargs):
        raise AssertionError("successful AI generation must not pre-resolve facts internally")

    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", unexpected_resolve)

    class Provider:
        settings = OpenAIPlannerSettings(base_url="http://provider", model="generator-model")

        async def recommend(self, facts, candidates, **_kwargs):
            from llm_d_bench.agentic.planner import DeterministicPlanner

            return DeterministicPlanner().decide(facts, candidates), DeterministicPlanner().score(facts, candidates)

    async def generate(*_args, **_kwargs):
        from llm_d_bench.agentic.generator import CandidateGenerationResult

        return CandidateGenerationResult(
            proposals={
                "candidates": [
                    {
                        "provider_ref": "baseline-vllm",
                        "replicas": 1,
                        "tensor_parallel_size": 1,
                        "rationale": "Fits.",
                    },
                    {
                        "provider_ref": "pd-disaggregation",
                        "replicas": 2,
                        "tensor_parallel_size": 2,
                        "prefill_replicas": 2,
                        "prefill_tensor_parallel_size": 2,
                        "rationale": "Too large.",
                    },
                ]
            },
            resolved=_resolved_facts(),
        )

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: Provider())
    monkeypatch.setattr("llm_d_bench.agentic.service.AICandidateGenerator.generate", generate)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1")))

    assert run.generator == "ai-mcp"
    assert run.generator_model == "generator-model"
    assert run.generator_fallback_reason is None
    assert [proposal["provider_ref"] for proposal in run.generator_candidate_proposals] == [
        "baseline-vllm",
        "pd-disaggregation",
    ]
    assert run.generator_candidate_proposals[1]["rationale"] == "Too large."
    assert [candidate.id for candidate in run.candidates] == ["baseline-vllm-tp1-r1"]
    assert len(run.rejected_candidates) == 1
    assert run.rejected_candidates[0].rejection_reasons == ["insufficient free accelerator cards"]


def test_create_scores_successful_ai_generated_candidates_without_resource_policy(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    captured: dict = {}
    score_calls: list[list[str]] = []
    original_score = DeterministicPlanner.score

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

    async def generate(*_args, **_kwargs):
        from llm_d_bench.agentic.generator import CandidateGenerationResult

        return CandidateGenerationResult(
            proposals={
                "candidates": [
                    {"provider_ref": "baseline-vllm", "replicas": 1, "tensor_parallel_size": 1, "rationale": "Fits."},
                    {"provider_ref": "baseline-vllm", "replicas": 1, "tensor_parallel_size": 2, "rationale": "Scales."},
                ]
            },
            resolved=_resolved_facts(
                free_gpu_count=2,
                aic_predictions=(
                    AICCandidatePrediction(
                        mode="agg", tensor_parallel_size=1, replicas=1, throughput_tokens_per_sec=100
                    ),
                    AICCandidatePrediction(
                        mode="agg", tensor_parallel_size=2, replicas=1, throughput_tokens_per_sec=200
                    ),
                ),
            ),
        )

    def score_spy(self, facts, candidates):
        score_calls.append([candidate.id for candidate in candidates])
        return original_score(self, facts, candidates)

    monkeypatch.setattr("llm_d_bench.agentic.service.AICandidateGenerator.generate", generate)
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id",
        lambda _id: OpenAICompatiblePlanner("http://provider", "planner"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.planner.DeterministicPlanner.score", score_spy)
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())
    async def validate_provider(_url):
        pass

    monkeypatch.setattr("llm_d_bench.ai_providers.service.validate_provider_endpoint", validate_provider)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1")))

    planner_context = captured["json"]["messages"][1]["content"].partition("\n\n")[0]
    supplied = json.loads(planner_context)["valid_candidates"]
    supplied_by_id = {candidate["id"]: candidate for candidate in supplied}
    assert run.generator == "ai-mcp"
    assert run.decision_metadata is not None
    assert run.decision_metadata.planner == "openai-compatible"
    assert score_calls
    assert score_calls[0] == ["baseline-vllm-tp1-r1", "baseline-vllm-tp2-r1"]
    assert "resource_policy" not in supplied_by_id["baseline-vllm-tp1-r1"]
    assert "resource_policy" not in supplied_by_id["baseline-vllm-tp2-r1"]


def test_create_uses_aggregate_inputs_only_for_deterministic_planning(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    captured = {}

    class Provider:
        async def recommend(self, facts, candidates, **_kwargs):
            captured["performance_score_inputs"] = facts.performance_score_inputs
            captured["baseline_deployable"] = next(
                candidate.deployable for candidate in candidates if candidate.id == "baseline-vllm-tp1-r1"
            )
            planner = DeterministicPlanner()
            return planner.decide(facts, candidates), planner.score(facts, candidates)

    def score_inputs(_self, _model, _facts, candidates):
        baseline = next(candidate for candidate in candidates if candidate.id == "baseline-vllm-tp1-r1")
        return (
            PerformanceScoreInput(
                candidate_id=baseline.id,
                source="benchmark",
                status="measured",
                confidence=1,
                effective_sample_size=3,
                has_blocking_negative_evidence=True,
            ),
        )

    deterministic_calls = []
    original_decide = DeterministicPlanner.decide

    def decide_spy(self, facts, candidates):
        deterministic_calls.append(facts.performance_score_inputs)
        return original_decide(self, facts, candidates)

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: Provider())
    monkeypatch.setattr("llm_d_bench.agentic.service.BenchmarkPerformanceEvidence.score_inputs", score_inputs)
    monkeypatch.setattr("llm_d_bench.agentic.planner.DeterministicPlanner.decide", decide_spy)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1")))

    assert run.decision_metadata is not None
    assert run.decision_metadata.planner == "openai-compatible"
    assert deterministic_calls[0][0].has_blocking_negative_evidence
    assert captured["performance_score_inputs"] == ()
    assert captured["baseline_deployable"] is True


def test_relaxed_candidate_gets_exact_estimate_before_scoring(monkeypatch):
    calls = []

    async def estimated(request):
        calls.append(request)
        return {"mode": "agg", "tp": 2, "replicas": 1, "ttft_ms": 180, "tpot_ms": 22}

    monkeypatch.setattr("llm_d_bench.agentic.service.estimate", estimated)
    facts = replace(_resolved_facts().facts, ttft_slo_ms=30)
    candidate = (
        DeterministicPlanner()
        .candidates(facts)[0]
        .model_copy(
            update={
                "id": "baseline-vllm-tp2-r1",
                "provider_ref": "baseline-vllm",
                "tensor_parallel_size": 2,
                "replicas": 1,
                "required_gpus": 2,
            }
        )
    )
    candidates, enriched = asyncio.run(
        AgenticDeploymentService._estimate_missing_topologies(
            "Qwen/Qwen3-8B",
            [candidate],
            facts,
            [
                {
                    "tool": "search_candidates",
                    "arguments": {
                        "workload": {"isl": 4096, "osl": 256, "ttftMs": 30, "tpotMs": 10},
                        "searchConfig": {"aicSystemName": "h100_sxm", "aicBackendName": "vllm"},
                    },
                }
            ],
        )
    )

    assert calls[0].gpu_count == 2
    assert calls[0].aic_system_name == "h100_sxm"
    assert calls[0].ttft_target_ms is None
    assert calls[0].tpot_target_ms is None
    assert candidates[0].performance_estimate["ttft_ms"] == 180
    assert candidates[0].performance_estimate_source == "aic_estimate"
    assert DeterministicPlanner()._slo_assessment(enriched, candidates[0])[0] == "not_satisfied"
    assert OpenAICompatiblePlanner._candidate_context(enriched, candidates[0])["performance_estimate"] == {
        "ttft_ms": 180,
        "tpot_ms": 22,
        "throughput_tokens_per_sec": None,
    }


def test_missing_aic_predictions_estimate_each_supported_candidate_for_ai_scoring(monkeypatch):
    calls = []

    async def estimated(request):
        calls.append(request)
        return {"ttft_ms": 180 + len(calls), "tpot_ms": 22, "throughput_tokens_per_sec": 1200}

    monkeypatch.setattr("llm_d_bench.agentic.service.estimate", estimated)
    facts = _resolved_facts(free_gpu_count=4).facts
    candidates = DeterministicPlanner().candidates(facts)
    supported = [
        next(candidate for candidate in candidates if candidate.provider_ref == provider_ref)
        for provider_ref in ("baseline-vllm", "optimized-baseline", "pd-disaggregation")
    ]
    enriched_candidates, enriched_facts = asyncio.run(
        AgenticDeploymentService._estimate_missing_topologies(
            "Qwen/Qwen3-8B",
            supported,
            facts,
            [
                {
                    "tool": "search_candidates",
                    "arguments": {
                        "workload": {"isl": 1024, "osl": 256},
                        "searchConfig": {},
                    },
                }
            ],
        )
    )

    assert len(calls) == len(supported)
    for index, (candidate, request) in enumerate(zip(enriched_candidates, calls, strict=True), start=1):
        assert request.gpu_count == candidate.required_gpus
        assert request.tp == candidate.tensor_parallel_size
        assert request.replicas == candidate.replicas
        if candidate.provider_ref == "pd-disaggregation":
            assert request.prefill_tp == candidate.prefill_tensor_parallel_size
            assert request.prefill_replicas == candidate.prefill_replicas
        context = OpenAICompatiblePlanner._candidate_context(enriched_facts, candidate)
        assert context["performance_estimate_source"] == "aic_estimate"
        assert context["performance_estimate"] == {
            "ttft_ms": 180 + index,
            "tpot_ms": 22,
            "throughput_tokens_per_sec": 1200,
        }


def test_relaxed_candidate_keeps_unknown_performance_when_estimate_fails(monkeypatch):
    from llm_d_bench.aic.service import AICError

    async def unavailable(_request):
        raise AICError("no matching topology")

    monkeypatch.setattr("llm_d_bench.agentic.service.estimate", unavailable)
    facts = _resolved_facts().facts
    candidate = DeterministicPlanner().candidates(facts)[0]
    candidates, enriched = asyncio.run(
        AgenticDeploymentService._estimate_missing_topologies(
            "Qwen/Qwen3-8B",
            [candidate],
            facts,
            [{"tool": "search_candidates", "arguments": {"workload": {}, "searchConfig": {}}}],
        )
    )

    assert candidates[0].performance_estimate is None
    assert enriched.aic_predictions == ()


def test_create_falls_back_only_when_all_ai_generated_candidates_are_invalid(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class Provider:
        settings = OpenAIPlannerSettings(base_url="http://provider", model="generator-model")

        async def recommend(self, facts, candidates, **_kwargs):
            from llm_d_bench.agentic.planner import DeterministicPlanner

            return DeterministicPlanner().decide(facts, candidates), DeterministicPlanner().score(facts, candidates)

    async def generate(*_args, **_kwargs):
        from llm_d_bench.agentic.generator import CandidateGenerationResult

        return CandidateGenerationResult(
            proposals={
                "candidates": [
                    {
                        "provider_ref": "baseline-vllm",
                        "replicas": 8,
                        "tensor_parallel_size": 8,
                        "rationale": "Exceeds capacity.",
                    }
                ]
            },
            resolved=_resolved_facts(),
        )

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: Provider())
    monkeypatch.setattr("llm_d_bench.agentic.service.AICandidateGenerator.generate", generate)
    progress = []

    async def on_progress(event):
        progress.append(event)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1"), on_progress=on_progress))

    assert run.generator == "deterministic"
    assert run.generator_fallback_reason == "all_ai_candidates_invalid"
    assert run.candidates
    assert run.rejected_candidates[0].id == "baseline-vllm-tp8-r8"
    assert any(
        event["phase"] == "generation"
        and event["status"] == "fallback"
        and "deterministic generation" in event["message"]
        for event in progress
    )


@pytest.mark.parametrize(("error_message", "reason", "detail"), [
    ('MCP tool "search_candidates" failed: unavailable', "mcp_unavailable", None),
    ("AI candidate generation returned too many tool calls in round 1: 4 (limit 3)",
     "invalid_response", "AI candidate generation returned too many tool calls in round 1: 4 (limit 3)"),
])
def test_create_falls_back_when_ai_generator_mcp_is_unavailable(monkeypatch, tmp_path, error_message, reason, detail):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class Provider:
        settings = OpenAIPlannerSettings(base_url="http://provider", model="generator-model")

        async def recommend(self, facts, candidates, **_kwargs):
            from llm_d_bench.agentic.planner import DeterministicPlanner

            return DeterministicPlanner().decide(facts, candidates), DeterministicPlanner().score(facts, candidates)

    async def generate(*_args, **_kwargs):
        from llm_d_bench.agentic.generator import CandidateGenerationError

        raise CandidateGenerationError(error_message)

    estimate_requests = []

    async def estimated(request):
        estimate_requests.append(request)
        return {"ttft_ms": 150, "tpot_ms": 20, "throughput_tokens_per_sec": 100}

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: Provider())
    monkeypatch.setattr("llm_d_bench.agentic.service.AICandidateGenerator.generate", generate)
    monkeypatch.setattr("llm_d_bench.agentic.service.estimate", estimated)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1")))

    assert run.generator == "deterministic"
    assert run.generator_fallback_reason == reason
    assert run.generator_fallback_detail == detail
    assert len(estimate_requests) == 3
    assert all(candidate.performance_estimate_source == "aic_estimate" for candidate in run.candidates)
    snapshot = json.loads((tmp_path / run.planning_snapshot_path).read_text(encoding="utf-8"))
    assert snapshot["generator"]["fallback_detail"] == detail
    assert all(candidate["performance_estimate_source"] == "aic_estimate" for candidate in snapshot["candidate_catalog"])


def test_missing_requested_provider_estimates_fallback_candidates(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: None)
    estimate_requests = []

    async def estimated(request):
        estimate_requests.append(request)
        return {"ttft_ms": 150, "tpot_ms": 20}

    monkeypatch.setattr("llm_d_bench.agentic.service.estimate", estimated)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1")))

    assert run.generator_fallback_reason == "provider_unavailable"
    assert len(estimate_requests) == 3
    assert all(candidate.performance_estimate_source == "aic_estimate" for candidate in run.candidates)


def test_generator_provider_http_error_is_not_classified_as_invalid_candidate_response():
    from llm_d_bench.agentic.generator import CandidateGenerationError

    error = CandidateGenerationError(
        "AI candidate generation failed: OpenAI-compatible provider request failed: "
        "Client error '400 Bad Request' for url 'https://api.deepseek.com/v1/chat/completions'"
    )

    assert AgenticDeploymentService._generator_failure_reason(error) == "provider_unavailable"


def test_generator_invalid_initial_tool_batch_is_classified_as_invalid_response():
    from llm_d_bench.agentic.generator import CandidateGenerationError

    error = CandidateGenerationError(
        "AI candidate generation must first call only get_cluster_overview and AIC search_candidates"
    )

    assert AgenticDeploymentService._generator_failure_reason(error) == "invalid_response"


def test_refine_keeps_provider_selected_candidates(monkeypatch, tmp_path):
    service = AgenticDeploymentService(snapshot_store=PlanningSnapshotStore(tmp_path))
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class Provider:
        async def recommend(self, _facts, candidates, **_kwargs):
            selected = [
                candidate.model_copy(update={"score": 1 - index / 10, "score_source": "openai-compatible"})
                for index, candidate in enumerate(candidates[:10])
            ]
            from llm_d_bench.agentic.planner import PlannerDecision

            return PlannerDecision(
                action="select_candidate", candidate_id=selected[0].id, confidence=0.8, rationale="provider choice"
            ), selected

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment", lambda: Provider())
    run = asyncio.run(service.create(_request()))
    created_path = tmp_path / run.planning_snapshot_path
    created_payload = created_path.read_text(encoding="utf-8")
    from llm_d_bench.agentic.models import AgenticCandidateRefinementRequest

    refined = asyncio.run(service.refine(run.id, AgenticCandidateRefinementRequest(planner_prompt="more capacity")))
    assert 1 <= len(refined.candidates) <= 10
    refined_path = tmp_path / refined.planning_snapshot_path
    assert refined_path != created_path
    assert json.loads(created_payload)["event"] == "created"
    assert json.loads(refined_path.read_text(encoding="utf-8"))["event"] == "refined"
    assert created_path.read_text(encoding="utf-8") == created_payload


def test_refine_regenerates_candidates_with_ai_and_mcp(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)
    prompts = []

    class Provider:
        settings = OpenAIPlannerSettings(base_url="http://provider", model="generator-model")

        async def recommend(self, facts, candidates, **_kwargs):
            from llm_d_bench.agentic.planner import DeterministicPlanner

            return DeterministicPlanner().decide(facts, candidates), DeterministicPlanner().score(facts, candidates)

    async def generate(_self, _cluster_id, _model, _facts, operator_prompt, on_progress=None):
        from llm_d_bench.agentic.generator import CandidateGenerationResult

        prompts.append(operator_prompt)
        return CandidateGenerationResult(
            proposals={
                "candidates": [
                    {
                        "provider_ref": "baseline-vllm",
                        "replicas": 1,
                        "tensor_parallel_size": 1,
                        "rationale": "Fits the refreshed preference.",
                    }
                ]
            },
            resolved=_resolved_facts(),
        )

    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: Provider())
    monkeypatch.setattr("llm_d_bench.agentic.service.AICandidateGenerator.generate", generate)

    run = asyncio.run(service.create(_request(ai_provider_id="provider-1", planner_prompt="balanced")))
    refined = asyncio.run(
        service.refine(
            run.id,
            AgenticCandidateRefinementRequest(planner_prompt="prefer fewer replicas"),
        )
    )

    assert prompts == ["balanced", "prefer fewer replicas"]
    assert refined.generator == "ai-mcp"
    assert refined.generator_model == "generator-model"
    assert refined.generator_fallback_reason is None
    assert refined.planning_trace[-1]["candidate_id"] == refined.selected_candidate.id
    assert refined.planning_trace[3]["ranking"]


def test_refine_falls_back_to_deterministic_planning_when_provider_fails(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", _resolve_facts)

    class FailingProvider:
        async def recommend(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment", lambda: FailingProvider()
    )
    run = asyncio.run(service.create(_request()))
    refined = asyncio.run(
        service.refine(
            run.id,
            AgenticCandidateRefinementRequest(planner_prompt="distributed"),
        )
    )

    assert refined.planner == "deterministic"
    assert refined.planner_fallback_reason is not None
    assert refined.decision_metadata.planner == "deterministic"


def test_refine_allows_external_ranking_of_exact_aic_candidates(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    resolved = _resolved_facts()
    resolved.facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
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

    async def resolve(*_args, **_kwargs):
        return resolved

    class Provider:
        async def recommend(self, _facts, candidates, **_kwargs):
            selected = [
                candidate.model_copy(update={"score": 0.9, "score_source": "openai-compatible"})
                for candidate in candidates[:2]
            ]
            from llm_d_bench.agentic.planner import PlannerDecision

            return PlannerDecision(
                action="select_candidate", candidate_id=selected[0].id, confidence=0.8, rationale="preference choice"
            ), selected

    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", resolve)
    monkeypatch.setattr("llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment", lambda: Provider())

    run = asyncio.run(service.create(_request()))
    refined = asyncio.run(
        service.refine(
            run.id,
            AgenticCandidateRefinementRequest(planner_prompt="distributed"),
        )
    )

    assert refined.planner == "openai-compatible"
    assert refined.decision.rationale == "preference choice"


def test_create_allows_external_planning_when_an_exact_aic_recommendation_exists(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )
    resolved = _resolved_facts()
    resolved.facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
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

    async def resolve(*_args, **_kwargs):
        return resolved

    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", resolve)

    class AvailableProvider:
        async def recommend(self, _facts, candidates, **_kwargs):
            selected = [
                candidate.model_copy(update={"score": 0.9, "score_source": "openai-compatible"})
                for candidate in candidates[:2]
            ]
            from llm_d_bench.agentic.planner import PlannerDecision

            return PlannerDecision(
                action="select_candidate", candidate_id=selected[0].id, confidence=0.8, rationale="preference choice"
            ), selected

    monkeypatch.setattr(
        "llm_d_bench.agentic.service.OpenAICompatiblePlanner.from_environment",
        lambda: AvailableProvider(),
    )

    run = asyncio.run(service.create(_request()))

    assert run.planner == "openai-compatible"
    assert run.selected_candidate.id == "baseline-vllm-tp1-r1"
    assert all(candidate.score_source == "openai-compatible" for candidate in run.candidates)
    assert len(run.candidates) == 2


def test_create_requires_a_model_cache_volume(monkeypatch):
    service = AgenticDeploymentService()
    monkeypatch.setattr(
        service,
        "_validate_model_cache",
        _ORIGINAL_VALIDATE_MODEL_CACHE,
    )
    monkeypatch.setattr(
        "llm_d_bench.agentic.service.require_active_session",
        lambda session_id: SimpleNamespace(id=session_id, server_id="cluster-a"),
    )

    with pytest.raises(ValueError, match="requires a model cache storage volume"):
        asyncio.run(service.create(_request(storage_volume_id="")))


@pytest.mark.parametrize(
    ("token_source", "expected"),
    [
        (
            {"mode": "existing-secret", "namespace": "models", "name": "hf-token"},
            {"mode": "existing-secret", "sourceNamespace": "models", "sourceName": "hf-token"},
        ),
        # Model Cache ``host`` is best-effort; Deploy ``host`` is not, and
        # mapping it would disable the cluster default Secret fallback.
        ({"mode": "host"}, None),
        ({"mode": "none"}, None),
    ],
    ids=["existing-secret", "host", "none"],
)
def test_validate_model_cache_maps_only_existing_secret_token_source(monkeypatch, token_source, expected):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry
    from llm_d_bench.storage.contracts import StorageVolumeKind, StorageVolumePurpose

    entry = ModelCacheEntry.model_validate(
        {
            "clusterId": "cluster-a",
            "storageVolumeId": "vol-1",
            "status": "ready",
            "cachePath": "hub/models--Qwen--Qwen3-8B",
            "source": {"kind": "huggingface", "huggingface": {"repoId": "Qwen/Qwen3-8B", "revision": "main"}},
            "tokenSource": token_source,
        }
    )
    volume = SimpleNamespace(purposes=[StorageVolumePurpose.MODEL_CACHE], kind=StorageVolumeKind.NFS)

    async def get_ready_volume(*_args, **_kwargs):
        return volume

    class _CacheService:
        async def list(self, **_kwargs):
            return [entry]

    monkeypatch.setattr("llm_d_bench.storage.service.get_ready_volume", get_ready_volume)
    monkeypatch.setattr("llm_d_bench.model_cache.service.default_service", lambda: _CacheService())

    result = asyncio.run(
        _ORIGINAL_VALIDATE_MODEL_CACHE(
            _request(storage_type="model-cache", storage_volume_id="vol-1"),
            "cluster-a",
        )
    )

    assert (result.model_dump(mode="json", by_alias=True) if result else None) == expected


def test_approve_rejects_a_candidate_when_live_cluster_capacity_has_changed(monkeypatch):
    service = AgenticDeploymentService()
    session = SimpleNamespace(id="a" * 36, server_id="cluster-a")
    monkeypatch.setattr("llm_d_bench.agentic.service.require_active_session", lambda _session_id: session)
    resolved = iter([_resolved_facts(free_gpu_count=4), _resolved_facts(free_gpu_count=0)])

    async def resolve(*_args, **_kwargs):
        return next(resolved)

    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", resolve)
    run = asyncio.run(service.create(_request()))

    with pytest.raises(ValueError, match="sufficient free accelerator cards"):
        asyncio.run(service.approve(run.id))


def test_approve_rejects_a_candidate_when_preflight_capacity_validation_fails(monkeypatch):
    service = AgenticDeploymentService()
    session = SimpleNamespace(id="a" * 36, server_id="cluster-a")
    monkeypatch.setattr("llm_d_bench.agentic.service.require_active_session", lambda _session_id: session)

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
    # First call: cluster has 80GB VRAM, fits
    normal = _resolved_facts(free_gpu_count=4)
    # Second call (during approve): cluster VRAM drops to 8GB, impossible to fit 20GB weights + activation
    oom_facts = _resolved_facts(free_gpu_count=4)
    oom_facts.facts = replace(
        oom_facts.facts,
        vram_per_gpu_gib=8.0,
        model_name="meta-llama/Llama-3-8B",
        model_config_dict=llama_cfg,
    )

    resolved = iter([normal, oom_facts])

    async def resolve(*_args, **_kwargs):
        return next(resolved)

    monkeypatch.setattr("llm_d_bench.agentic.service.resolve_planning_facts", resolve)
    run = asyncio.run(service.create(_request()))

    with pytest.raises(ValueError, match="(fails pre-flight capacity validation|satisfies current resource constraints)"):
        asyncio.run(service.approve(run.id))
