"""Tests for AI candidate generation and deterministic validation."""

import asyncio
import json

import pytest

from llm_d_bench.agentic.generator import (
    AgenticMcpClient,
    AICandidateGenerator,
    CandidateGenerationError,
    CandidateProposal,
    CandidateValidator,
)
from llm_d_bench.agentic.planner import OpenAIPlannerSettings, PlanningFacts, WorkloadProfile
from llm_d_bench.ai_providers.client import AIToolCall, AIToolTurn
from llm_d_bench.auth.security import verify_internal


def test_validator_keeps_valid_ai_candidates_when_other_proposals_are_invalid():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        evidence_ids=("aic:search",),
    )
    proposals = [
        CandidateProposal(
            provider_ref="baseline-vllm",
            replicas=1,
            tensor_parallel_size=1,
            rationale="Small deployable baseline.",
            evidence_ids=["aic:search"],
        ),
        CandidateProposal(
            provider_ref="pd-disaggregation",
            replicas=2,
            tensor_parallel_size=2,
            prefill_replicas=2,
            prefill_tensor_parallel_size=2,
            rationale="High-throughput topology.",
        ),
    ]

    result = CandidateValidator().validate(facts, proposals)

    assert [candidate.id for candidate in result.accepted] == ["baseline-vllm-tp1-r1"]
    assert result.accepted[0].evidence_ids == ["aic:search"]
    assert [candidate.id for candidate in result.rejected] == ["pd-disaggregation-p2-tp2-d2-tp2"]
    assert result.rejected[0].rejection_reasons == ["insufficient free accelerator cards"]


def test_mcp_client_signs_authenticated_principal(monkeypatch):
    monkeypatch.setenv("LENS_INTERNAL_AUTH_SECRET", "test-secret")
    monkeypatch.setattr("llm_d_bench.agentic.generator.time.time", lambda: 1000)

    headers = AgenticMcpClient("https://127.0.0.1:3005/api/mcp", principal_id="user-1")._headers()

    assert headers["x-prism-principal-id"] == "user-1"
    assert verify_internal(
        "test-secret",
        signature=headers["x-prism-internal-sig"],
        timestamp=1000,
        method="POST",
        path="/api/mcp",
        principal_id="user-1",
        now=1000,
    )


def test_validator_drops_ai_evidence_ids_not_present_in_authoritative_facts():
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        evidence_ids=("cluster-overview:cluster-a",),
    )
    proposal = CandidateProposal(
        provider_ref="baseline-vllm",
        replicas=1,
        tensor_parallel_size=1,
        rationale="Fits.",
        evidence_ids=["cluster-overview:cluster-a", "invented:evidence"],
    )

    result = CandidateValidator().validate(facts, [proposal])

    assert result.accepted[0].evidence_ids == ["cluster-overview:cluster-a"]


def test_candidate_proposal_normalizes_pd_decode_aliases():
    proposal = CandidateProposal.model_validate(
        {
            "provider_ref": "pd-disaggregation",
            "decode_replicas": 2,
            "decode_tensor_parallel_size": 1,
            "prefill_replicas": 1,
            "prefill_tensor_parallel_size": 1,
            "rationale": "Distributed topology.",
        }
    )

    assert proposal.replicas == 2
    assert proposal.tensor_parallel_size == 1


def test_validator_rejects_incomplete_pd_topology():
    facts = PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=8)
    proposal = CandidateProposal(
        provider_ref="pd-disaggregation",
        replicas=1,
        tensor_parallel_size=1,
        rationale="Incomplete proposal.",
    )

    result = CandidateValidator().validate(facts, [proposal])

    assert result.accepted == []
    assert result.rejected[0].rejection_reasons == [
        "PD candidates require both prefill replicas and tensor parallelism",
    ]


def _cluster_tool():
    return {
        "name": "get_cluster_overview",
        "description": "[read] Get cluster.",
        "input_schema": {"type": "object", "properties": {"clusterId": {"type": "string"}}},
    }


def _cluster_result():
    return {
        "hardware": {
            "gpuCount": 4,
            "availableGpuCount": 4,
            "vramBytes": 4 * 32 * 1024**3,
            "memoryBytes": 128 * 1024**3,
            "memoryUsagePercent": 25,
        }
    }


def _aic_tool():
    return {
        "name": "search_candidates",
        "description": "[read] Search AIC candidates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workload": {"type": "object"},
                "searchConfig": {"type": "object"},
                "sourceIds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["workload", "searchConfig", "sourceIds"],
        },
    }


def _aic_arguments():
    return {
        "workload": {"model": "Qwen/Qwen3-8B", "isl": 1024, "osl": 256},
        "searchConfig": {"totalGpus": 4},
        "sourceIds": ["aic"],
    }


def _aic_result(*, ttft_ms=300, tpot_ms=30):
    return {
        "candidates": [
            {
                "source": "aic",
                "topologyMode": "agg",
                "decodeTp": 1,
                "decodeReplicas": 1,
                "predicted": {"ttftMs": ttft_ms, "tpotMs": tpot_ms, "throughputTps": 1000},
            }
        ]
    }


def _mcp_cluster_result():
    return {
        "cluster": {"id": "cluster-a"},
        "kubernetes": {
            "hardware": {
                "gpuCount": 8,
                "availableGpuCount": 6,
                "vramBytes": 8 * 24 * 1024**3,
                "memoryBytes": 256 * 1024**3,
                "memoryUsagePercent": 25,
            },
            "nodes": [],
        },
    }


def test_resolve_mcp_facts_accepts_nested_cluster_overview_hardware():
    resolved = AICandidateGenerator._resolve_mcp_facts(
        "cluster-a",
        PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=1, free_gpu_count=0),
        {"get_cluster_overview": [_mcp_cluster_result()]},
    )

    assert resolved.facts.vram_per_gpu_gib == 24
    assert resolved.facts.free_gpu_count == 6
    assert resolved.facts.cpu_buffer_gib == 192
    assert resolved.cluster_snapshot["cluster"]["id"] == "cluster-a"


def test_ai_generator_gets_required_evidence_via_model_selected_mcp_call(monkeypatch):
    captured = {"tool_args": []}

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, arguments):
            captured["tool_args"].append((name, arguments))
            return _cluster_result() if name == "get_cluster_overview" else _aic_result()

    class Provider:
        calls = 0

        async def complete_tool_turn(self, **kwargs):
            captured.update(kwargs)
            self.calls += 1
            if self.calls == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="call-1", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="call-2", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit-1",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Fits the current cluster.",
                                    "evidence_ids": ["cluster-overview:cluster-a"],
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )
    facts = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=32,
        free_gpu_count=4,
        evidence_ids=("aic:search",),
    )

    proposals = asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", facts, "prefer throughput"))

    assert captured["tool_args"] == [
        ("get_cluster_overview", {"clusterId": "cluster-a"}),
        ("search_candidates", _aic_arguments()),
    ]
    assert "The server will independently recompute" in captured["system_prompt"]
    assert "Do not score, select, render, approve, or deploy" in captured["system_prompt"]
    user_context = json.loads(captured["messages"][0]["content"])
    assert user_context["operator_preference"] == "prefer throughput"
    assert "validated_planning_facts" not in user_context
    assert proposals.proposals.candidates[0].provider_ref == "baseline-vllm"
    assert proposals.resolved.facts.vram_per_gpu_gib == 32
    assert proposals.resolved.facts.free_gpu_count == 4


def test_ai_generator_relaxes_only_after_empty_aic_search_and_freezes_original_targets(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool(), {**_aic_tool(), "name": "search_aic_with_relaxation"}]

        async def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "get_cluster_overview":
                return _cluster_result()
            if name == "search_candidates":
                return {"candidates": []}
            anchor = {**_aic_result(ttft_ms=600)["candidates"][0], "decodeTp": 2, "totalGpus": 2}
            return {
                "candidates": [anchor],
                "anchor": anchor,
                "sloSatisfied": False,
            }

    class Provider:
        rounds = 0

        async def complete_tool_turn(self, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                assert "search_aic_with_relaxation" not in [
                    AICandidateGenerator._provider_tool_name(tool) for tool in kwargs["tools"]
                ]
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            if self.rounds == 2:
                assert "search_aic_with_relaxation" in [
                    AICandidateGenerator._provider_tool_name(tool) for tool in kwargs["tools"]
                ]
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(
                            id="aic",
                            name="search_aic_with_relaxation",
                            arguments={
                                "clusterId": "wrong",
                                "workload": {"ttftMs": 1000},
                                "searchConfig": {"totalGpus": 64},
                            },
                        ),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 2,
                                    "tensor_parallel_size": 1,
                                    "rationale": "AIC anchor requires validation.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    result = asyncio.run(
        AICandidateGenerator(
            OpenAIPlannerSettings(base_url="http://provider", model="planner"),
            tools=Tools(),
        ).generate("cluster-a", "model", PlanningFacts(8, 32, 4, ttft_slo_ms=500), None)
    )

    assert calls[1] == (
        "search_candidates",
        _aic_arguments()
        | {
            "workload": {
                "model": "model",
                "isl": 1024,
                "osl": 256,
                "ttftMs": 500,
            }
        },
    )
    assert calls[2] == (
        "search_aic_with_relaxation",
        {
            "clusterId": "cluster-a",
            "workload": {"model": "model", "isl": 1024, "osl": 256, "ttftMs": 500},
            "searchConfig": {"capacityMode": "free", "customGpuCount": None},
        },
    )
    assert result.resolved.facts.aic_predictions == ()
    assert result.proposals.candidates[0].replicas == 2
    assert [
        (candidate.provider_ref, candidate.tensor_parallel_size, candidate.replicas)
        for candidate in result.proposals.candidates
    ] == [
        ("baseline-vllm", 1, 2),
        ("baseline-vllm", 2, 1),
    ]
    accepted = CandidateValidator().validate(result.resolved.facts, result.proposals.candidates).accepted
    assert accepted[-1].required_gpus == 2


@pytest.mark.parametrize(
    "capacity_mode,custom_gpu_count,expected_budget",
    [
        ("free", None, 3),
        ("all", None, 8),
        ("custom", 2, 2),
    ],
)
def test_ai_generator_relaxes_no_feasible_aic_error_with_live_gpu_budget(
    monkeypatch,
    capacity_mode,
    custom_gpu_count,
    expected_budget,
):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool(), {**_aic_tool(), "name": "search_aic_with_relaxation"}]

        async def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "get_cluster_overview":
                cluster = _mcp_cluster_result()
                cluster["kubernetes"]["hardware"]["availableGpuCount"] = 3
                return cluster
            if name == "search_candidates":
                return {
                    "error": "upstream request failed (status 400)",
                    "detail": {"error": "No feasible configurations found for the given parameters."},
                }
            return {"candidates": [], "anchor": None}

    class Provider:
        rounds = 0

        async def complete_tool_turn(self, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(
                            id="aic",
                            name="search_candidates",
                            arguments={
                                **_aic_arguments(),
                                "searchConfig": {"totalGpus": 8},
                            },
                        ),
                    ]
                )
            if self.rounds == 2:
                assert "search_aic_with_relaxation" in [
                    AICandidateGenerator._provider_tool_name(tool) for tool in kwargs["tools"]
                ]
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(
                            id="relaxed",
                            name="search_aic_with_relaxation",
                            arguments={
                                "clusterId": "cluster-a",
                                "workload": {},
                                "searchConfig": {},
                            },
                        )
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "No AIC prediction is available.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    result = asyncio.run(
        AICandidateGenerator(
            OpenAIPlannerSettings(base_url="http://provider", model="planner"),
            tools=Tools(),
        ).generate(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                8,
                24,
                0,
                hardware_capacity_mode=capacity_mode,
                custom_gpu_count=custom_gpu_count,
            ),
            None,
        )
    )

    assert calls[1][0] == "search_candidates"
    assert calls[1][1]["searchConfig"]["totalGpus"] == expected_budget
    assert calls[2][0] == "search_aic_with_relaxation"
    assert result.proposals.tool_trace[1]["result"]["candidates"] == []
    assert (
        result.proposals.tool_trace[1]["result"]["detail"]["error"]
        == "No feasible configurations found for the given parameters."
    )
    assert result.resolved.facts.free_gpu_count == expected_budget


def test_ai_generator_keeps_relaxed_tool_hidden_when_aic_returns_candidates(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool(), {**_aic_tool(), "name": "search_aic_with_relaxation"}]

        async def call(self, name, arguments):
            calls.append(name)
            return _cluster_result() if name == "get_cluster_overview" else _aic_result(ttft_ms=600)

    class Provider:
        rounds = 0

        async def complete_tool_turn(self, **kwargs):
            self.rounds += 1
            assert "search_aic_with_relaxation" not in [
                AICandidateGenerator._provider_tool_name(tool) for tool in kwargs["tools"]
            ]
            if self.rounds == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Initial AIC search returned a candidate.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    asyncio.run(
        AICandidateGenerator(
            OpenAIPlannerSettings(base_url="http://provider", model="planner"),
            tools=Tools(),
        ).generate("cluster-a", "model", PlanningFacts(8, 32, 4, ttft_slo_ms=500), None)
    )

    assert calls == ["get_cluster_overview", "search_candidates"]


def test_ai_generator_does_not_relax_unrelated_aic_error(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool(), {**_aic_tool(), "name": "search_aic_with_relaxation"}]

        async def call(self, name, _arguments):
            calls.append(name)
            if name == "get_cluster_overview":
                return _cluster_result()
            return {"error": "upstream request failed (status 400)", "detail": {"error": "Model not supported"}}

    class Provider:
        rounds = 0

        async def complete_tool_turn(self, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            assert "search_aic_with_relaxation" not in [
                AICandidateGenerator._provider_tool_name(tool) for tool in kwargs["tools"]
            ]
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "No prediction available.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    result = asyncio.run(
        AICandidateGenerator(
            OpenAIPlannerSettings(base_url="http://provider", model="planner"),
            tools=Tools(),
        ).generate("cluster-a", "model", PlanningFacts(8, 32, 4), None)
    )

    assert calls == ["get_cluster_overview", "search_candidates"]
    assert "candidates" not in result.proposals.tool_trace[1]["result"]


def test_ai_generator_does_not_search_when_no_gpus_are_available(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            calls.append(name)
            cluster = _cluster_result()
            cluster["hardware"]["availableGpuCount"] = 0
            return cluster

    class Provider:
        async def complete_tool_turn(self, **_kwargs):
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(id="cluster", name="get_cluster_overview", arguments={}),
                    AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    with pytest.raises(CandidateGenerationError, match="No GPUs available"):
        asyncio.run(
            AICandidateGenerator(
                OpenAIPlannerSettings(base_url="http://provider", model="planner"),
                tools=Tools(),
            ).generate("cluster-a", "model", PlanningFacts(8, 32, 0), None)
        )
    assert calls == ["get_cluster_overview"]


def test_ai_generator_requires_relaxed_search_after_empty_aic_result(monkeypatch):
    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool(), {**_aic_tool(), "name": "search_aic_with_relaxation"}]

        async def call(self, name, _arguments):
            return _cluster_result() if name == "get_cluster_overview" else {"candidates": []}

    class Provider:
        rounds = 0

        async def complete_tool_turn(self, **_kwargs):
            self.rounds += 1
            if self.rounds == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "No AIC result.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    with pytest.raises(CandidateGenerationError, match="skipped relaxed AIC search"):
        asyncio.run(
            AICandidateGenerator(
                OpenAIPlannerSettings(base_url="http://provider", model="planner"),
                tools=Tools(),
            ).generate("cluster-a", "model", PlanningFacts(8, 32, 4), None)
        )


def test_ai_generator_does_not_replay_an_empty_assistant_response(monkeypatch):
    request_messages = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            return _cluster_result() if name == "get_cluster_overview" else _aic_result()

    class Provider:
        calls = 0

        async def complete_tool_turn(self, **kwargs):
            self.calls += 1
            request_messages.append(json.loads(json.dumps(kwargs["messages"])))
            if self.calls == 1:
                return AIToolTurn()
            if self.calls == 2:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Fits the observed cluster.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    result = asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))

    assert result.proposals.candidates[0].provider_ref == "baseline-vllm"
    assert request_messages[1] == [
        request_messages[0][0],
        {
            "role": "user",
            "content": (
                "Your response did not call a tool. Continue by calling one provided tool; do not reply with text only."
            ),
        },
    ]


def test_ai_generator_runs_bounded_optional_mcp_calls_before_final_candidates(monkeypatch):
    calls = []
    exposed_tools = []

    class Tools:
        async def available_tools(self):
            return [
                _cluster_tool(),
                _aic_tool(),
                {
                    "name": "list_simulation_tasks",
                    "description": "[read] List simulation history.",
                    "input_schema": {"type": "object", "properties": {"model": {"type": "string"}}},
                },
            ]

        async def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "get_cluster_overview":
                return _cluster_result()
            if name == "search_candidates":
                return {"candidates": []}
            return {"items": [{"id": "simulation-1", "status": "completed"}]}

    class Provider:
        turns = 0

        async def complete_tool_turn(self, **kwargs):
            self.turns += 1
            exposed_tools.append([AICandidateGenerator._provider_tool_name(tool) for tool in kwargs["tools"]])
            if self.turns == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="call-1", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="call-2", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            if self.turns == 2:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(
                            id="call-3",
                            name="list_simulation_tasks",
                            arguments={"model": "Qwen/Qwen3-8B"},
                        )
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit-1",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "optimized-baseline",
                                    "replicas": 2,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Uses simulation history.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    proposals = asyncio.run(
        generator.generate(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(8, 32, 4),
            "prefer throughput",
        )
    )

    assert calls == [
        ("get_cluster_overview", {"clusterId": "cluster-a"}),
        ("search_candidates", _aic_arguments()),
        ("list_simulation_tasks", {"model": "Qwen/Qwen3-8B"}),
    ]
    assert set(exposed_tools[0]) == {"get_cluster_overview", "search_candidates"}
    assert "list_simulation_tasks" in exposed_tools[1]
    assert "submit_candidate_proposals" in exposed_tools[1]
    assert proposals.proposals.candidates[0].provider_ref == "optimized-baseline"
    assert proposals.resolved.facts.aic_predictions == ()
    assert "aic:search" not in proposals.resolved.facts.evidence_ids
    assert proposals.proposals.tool_trace[2]["tool"] == "list_simulation_tasks"
    assert proposals.proposals.tool_trace[2]["status"] == "success"
    assert len(proposals.proposals.tool_trace[2]["arguments_digest"]) == 64
    assert len(proposals.proposals.tool_trace[2]["result_digest"]) == 64
    assert "arguments" not in proposals.proposals.tool_trace[2]
    assert "result" not in proposals.proposals.tool_trace[2]
    aic_trace = proposals.proposals.tool_trace[1]
    assert aic_trace["arguments"] == _aic_arguments()
    assert aic_trace["result"] == {"candidates": []}


def test_ai_generator_retries_one_text_only_turn(monkeypatch):
    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            return _cluster_result() if name == "get_cluster_overview" else {"candidates": []}

    class Provider:
        turns = 0

        async def complete_tool_turn(self, **kwargs):
            self.turns += 1
            if self.turns == 1:
                return AIToolTurn(content="I will inspect the cluster first.")
            if self.turns == 2:
                assert "did not call a tool" in kwargs["messages"][-1]["content"]
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="call-1", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="call-2", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit-1",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Fits.",
                                }
                            ]
                        },
                    )
                ]
            )

    provider = Provider()
    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: provider)
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    result = asyncio.run(
        generator.generate(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                8,
                32,
                4,
                ttft_slo_ms=40,
                workload_profile={"mean_input_tokens": 4096, "mean_output_tokens": 512},
            ),
            None,
        )
    )

    assert provider.turns == 3
    assert result.proposals.candidates[0].provider_ref == "baseline-vllm"


def test_ai_generator_rejects_tool_outside_planning_allowlist(monkeypatch):
    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, *_args):
            raise AssertionError("blocked tools must not reach MCP")

    class Provider:
        async def complete_tool_turn(self, **_kwargs):
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="call-1",
                        name="approve_agentic_plan",
                        arguments={"run_id": "run-1"},
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    with pytest.raises(CandidateGenerationError, match="not allowed"):
        asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))


def test_ai_generator_rejects_submission_without_cluster_evidence(monkeypatch):
    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, *_args):
            raise AssertionError("submission must not execute MCP tools")

    class Provider:
        async def complete_tool_turn(self, **_kwargs):
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit-1",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Unsupported guess.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    with pytest.raises(CandidateGenerationError, match="invalid response: cluster evidence was not gathered"):
        asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))


def test_ai_generator_enforces_per_round_mcp_call_limit(monkeypatch):
    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, *_args):
            raise AssertionError("oversized batch must be rejected before execution")

    class Provider:
        calls = 0

        async def complete_tool_turn(self, **_kwargs):
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(id=f"call-{index}", name="get_cluster_overview", arguments={"clusterId": "cluster-a"})
                    for index in range(4)
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    with pytest.raises(CandidateGenerationError, match=r"too many tool calls in round 1: 4 \(limit 3\)"):
        asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))


def test_ai_generator_allows_only_four_mcp_rounds(monkeypatch):
    class Tools:
        calls = 0

        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            self.calls += 1
            return _cluster_result() if name == "get_cluster_overview" else {"candidates": []}

    class Provider:
        calls = 0

        async def complete_tool_turn(self, **_kwargs):
            if self.calls == 0:
                self.calls += 1
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="repeat",
                        name="get_cluster_overview",
                        arguments={"clusterId": "cluster-a"},
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    tools = Tools()
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=tools,
    )

    with pytest.raises(CandidateGenerationError, match="tool-round limit"):
        asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))

    assert tools.calls == 5


def test_ai_generator_rejects_first_tool_batch_without_aic_search(monkeypatch):
    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, *_args):
            raise AssertionError("invalid first batch must not execute MCP tools")

    class Provider:
        turns = 0

        async def complete_tool_turn(self, **_kwargs):
            self.turns += 1
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="cluster",
                        name="get_cluster_overview",
                        arguments={"clusterId": "cluster-a"},
                    )
                ]
            )

    provider = Provider()
    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: provider)
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"), tools=Tools(),
    )

    with pytest.raises(CandidateGenerationError, match="must first call"):
        asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))

    assert provider.turns == 2


def test_ai_generator_retries_invalid_first_tool_batch_once(monkeypatch):
    calls = []
    requests = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            calls.append(name)
            return _cluster_result() if name == "get_cluster_overview" else _aic_result()

    class Provider:
        async def complete_tool_turn(self, **kwargs):
            requests.append(list(kwargs["messages"]))
            if len(requests) == 1:
                return AIToolTurn(tool_calls=[AIToolCall(
                    id="cluster-only", name="get_cluster_overview", arguments={"clusterId": "cluster-a"},
                )])
            if len(requests) == 2:
                return AIToolTurn(tool_calls=[
                    AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                    AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                ])
            return AIToolTurn(tool_calls=[AIToolCall(
                id="submit", name="submit_candidate_proposals", arguments={"candidates": [{
                    "provider_ref": "baseline-vllm", "replicas": 1, "tensor_parallel_size": 1,
                    "rationale": "Fits the observed cluster.",
                }]},
            )])

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    result = asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))

    assert len(requests) == 3
    assert "Retry once by calling exactly" in requests[1][-1]["content"]
    assert calls == ["get_cluster_overview", "search_candidates"]
    assert result.proposals.candidates[0].provider_ref == "baseline-vllm"


def test_ai_generator_injects_required_cluster_and_aic_arguments(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, arguments):
            calls.append((name, arguments))
            return _cluster_result() if name == "get_cluster_overview" else _aic_result()

    class Provider:
        turn = 0

        async def complete_tool_turn(self, **_kwargs):
            self.turn += 1
            if self.turn > 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(
                            id="submit",
                            name="submit_candidate_proposals",
                            arguments={
                                "candidates": [
                                    {
                                        "provider_ref": "baseline-vllm",
                                        "replicas": 1,
                                        "tensor_parallel_size": 1,
                                        "rationale": "Fits the observed cluster.",
                                    }
                                ]
                            },
                        )
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "wrong-cluster"}),
                    AIToolCall(
                        id="aic",
                        name="search_candidates",
                        arguments={
                            **_aic_arguments(),
                            "sourceIds": ["manual"],
                            "workload": {"ttftMs": 35, "tpotMs": 20},
                        },
                    ),
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    result = asyncio.run(
        generator.generate(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                8,
                32,
                4,
                ttft_slo_ms=40,
                workload_profile={"mean_input_tokens": 4096, "mean_output_tokens": 512},
            ),
            None,
        )
    )

    assert result.proposals.candidates[0].provider_ref == "baseline-vllm"
    assert calls[0] == ("get_cluster_overview", {"clusterId": "cluster-a"})
    assert calls[1][0] == "search_candidates"
    assert calls[1][1]["sourceIds"] == ["aic"]
    assert calls[1][1]["workload"] == {
        "model": "Qwen/Qwen3-8B",
        "isl": 4096,
        "osl": 512,
        "ttftMs": 40,
    }
    assert result.proposals.tool_trace[1]["arguments"]["workload"] == calls[1][1]["workload"]


def test_aic_tool_arguments_use_requested_workload_and_slo():
    calls = [
        AIToolCall(
            id="aic",
            name="search_candidates",
            arguments={
                "workload": {"isl": 8, "osl": 8, "ttftMs": 999, "tpotMs": 999},
            },
        )
    ]
    normalized = AICandidateGenerator._normalize_required_tool_arguments(
        calls,
        "cluster-a",
        "Qwen/Qwen3-0.6B",
        PlanningFacts(
            0.7,
            32,
            4,
            ttft_slo_ms=500,
            tpot_slo_ms=50,
            workload_profile={"mean_input_tokens": 1000, "mean_output_tokens": 200, "concurrency": 10},
        ),
    )
    assert normalized[0].arguments["workload"] == {
        "model": "Qwen/Qwen3-0.6B",
        "isl": 1000,
        "osl": 200,
        "ttftMs": 500,
        "tpotMs": 50,
    }


def test_preference_constraints_override_form_values_for_aic_and_slo_filter(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "get_cluster_overview":
                return _cluster_result()
            return {
                "candidates": [
                    *_aic_result(ttft_ms=600, tpot_ms=30)["candidates"],
                    *_aic_result(ttft_ms=200, tpot_ms=40)["candidates"],
                ]
            }

    class Provider:
        turn = 0

        async def complete_tool_turn(self, **_kwargs):
            self.turn += 1
            if self.turn == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={}),
                        AIToolCall(
                            id="aic",
                            name="search_candidates",
                            arguments={
                                **_aic_arguments(),
                                "workload": {"ttftMs": 900, "tpotMs": 900},
                            },
                        ),
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Fits the observed cluster.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )
    result = asyncio.run(
        generator.generate(
            "cluster-a",
            "Qwen/Qwen3-0.6B",
            PlanningFacts(
                0.7,
                32,
                4,
                ttft_slo_ms=300,
                tpot_slo_ms=20,
                workload_profile=WorkloadProfile(mean_input_tokens=4096, mean_output_tokens=512),
            ),
            "user requests have an ISL of 1000 and an OSL of 200, requiring a TTFT of <= 500ms "
            "and a TPOT of <= 50ms under 10 concurrent users.",
        )
    )

    assert calls[1][1]["workload"] == {
        "model": "Qwen/Qwen3-0.6B",
        "isl": 1000,
        "osl": 200,
        "ttftMs": 500,
        "tpotMs": 50,
    }
    assert result.proposals.tool_trace[1]["arguments"]["workload"] == calls[1][1]["workload"]
    assert result.resolved.facts.workload_profile.concurrency == 10
    assert result.resolved.facts.ttft_slo_ms == 500
    assert result.resolved.facts.tpot_slo_ms == 50
    assert result.resolved.trace[1]["aic_predictions_filtered_by_slo"] == 1
    assert len(result.resolved.facts.aic_predictions) == 1
    assert result.resolved.facts.aic_predictions[0].ttft_ms == 200


def test_preference_only_supplies_workload_and_slo_for_aic():
    facts = AICandidateGenerator._apply_preference_constraints(
        PlanningFacts(0.7, 32, 4, workload_profile=WorkloadProfile()),
        "ISL of 1000 and OSL of 200; TTFT of <= 500ms, TPOT of <= 50ms under 10 concurrent users",
    )
    tool = AIToolCall(id="aic", name="search_candidates", arguments={"workload": {}})

    assert AICandidateGenerator._normalize_required_tool_arguments(
        [tool],
        "cluster-a",
        "Qwen/Qwen3-0.6B",
        facts,
    )[0].arguments["workload"] == {
        "model": "Qwen/Qwen3-0.6B",
        "isl": 1000,
        "osl": 200,
        "ttftMs": 500,
        "tpotMs": 50,
    }
    assert facts.workload_profile.concurrency == 10


def test_preference_natural_language_upper_limits_override_aic_targets():
    facts = AICandidateGenerator._apply_preference_constraints(
        PlanningFacts(0.7, 32, 4, ttft_slo_ms=50, tpot_slo_ms=20),
        "TTFT less than 40, TPOT less than 10",
    )
    tool = AIToolCall(id="aic", name="search_candidates", arguments={"workload": {"ttftMs": 50, "tpotMs": 20}})

    assert facts.ttft_slo_ms == 40
    assert facts.tpot_slo_ms == 10
    assert AICandidateGenerator._normalize_required_tool_arguments(
        [tool],
        "cluster-a",
        "Qwen/Qwen3-0.6B",
        facts,
    )[0].arguments["workload"] == {
        "model": "Qwen/Qwen3-0.6B",
        "isl": 1024,
        "osl": 256,
        "ttftMs": 40,
        "tpotMs": 10,
    }


def test_preference_constraints_accept_legacy_dict_workload():
    facts = AICandidateGenerator._apply_preference_constraints(
        PlanningFacts(0.7, 32, 4, workload_profile={"mean_input_tokens": 4096}),
        "TTFT <= 500ms",
    )

    assert facts.workload_profile.mean_input_tokens == 4096
    assert facts.ttft_slo_ms == 500


def test_ai_generator_uses_aic_default_workload_when_none_is_provided(monkeypatch):
    calls = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, arguments):
            calls.append((name, arguments))
            return _cluster_result() if name == "get_cluster_overview" else _aic_result()

    class Provider:
        turn = 0

        async def complete_tool_turn(self, **_kwargs):
            self.turn += 1
            if self.turn > 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(
                            id="submit",
                            name="submit_candidate_proposals",
                            arguments={
                                "candidates": [
                                    {
                                        "provider_ref": "baseline-vllm",
                                        "replicas": 1,
                                        "tensor_parallel_size": 1,
                                        "rationale": "Fits the observed cluster.",
                                    }
                                ]
                            },
                        )
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(id="cluster", name="get_cluster_overview", arguments={}),
                    AIToolCall(
                        id="aic",
                        name="search_candidates",
                        arguments={
                            "workload": {"isl": 4096},
                            "searchConfig": {},
                            "sourceIds": ["aic"],
                        },
                    ),
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    asyncio.run(generator.generate("cluster-a", "Qwen/Qwen3-8B", PlanningFacts(8, 32, 4), None))

    assert calls[1] == (
        "search_candidates",
        {
            "workload": {"model": "Qwen/Qwen3-8B", "isl": 1024, "osl": 256},
            "searchConfig": {"totalGpus": 4},
            "sourceIds": ["aic"],
        },
    )


def test_ai_generator_retries_an_invalid_candidate_submission(monkeypatch):
    requests = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            return _cluster_result() if name == "get_cluster_overview" else _aic_result()

    class Provider:
        turn = 0

        async def complete_tool_turn(self, **kwargs):
            self.turn += 1
            requests.append(kwargs["messages"])
            if self.turn == 1:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ]
                )
            if self.turn == 2:
                return AIToolTurn(
                    tool_calls=[
                        AIToolCall(
                            id="invalid",
                            name="submit_candidate_proposals",
                            arguments={},
                        )
                    ]
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="valid",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "baseline-vllm",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Fits the observed cluster.",
                                }
                            ]
                        },
                    )
                ]
            )

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    result = asyncio.run(
        generator.generate(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(8, 32, 4),
            None,
        )
    )

    assert result.proposals.candidates[0].provider_ref == "baseline-vllm"
    correction = requests[2][-1]["results"][0]
    assert correction["tool_call_id"] == "invalid"
    assert "submission schema invalid" in correction["content"]


def test_resolve_mcp_facts_discards_aic_predictions_that_fail_or_cannot_prove_slo():
    requested = PlanningFacts(
        model_weight_gib=8,
        vram_per_gpu_gib=1,
        free_gpu_count=0,
        ttft_slo_ms=500,
        tpot_slo_ms=50,
    )
    aic_result = {
        "candidates": [
            *_aic_result(ttft_ms=600, tpot_ms=30)["candidates"],
            *_aic_result(ttft_ms=300, tpot_ms=None)["candidates"],
            *_aic_result(ttft_ms=300, tpot_ms=30)["candidates"],
        ]
    }

    resolved = AICandidateGenerator._resolve_mcp_facts(
        "cluster-a",
        requested,
        {"get_cluster_overview": [_cluster_result()], "search_candidates": [aic_result]},
    )

    assert len(resolved.facts.aic_predictions) == 1
    assert resolved.facts.aic_predictions[0].ttft_ms == 300
    assert resolved.facts.aic_predictions[0].tpot_ms == 30
    assert "aic:search" in resolved.facts.evidence_ids


def test_ai_generator_prompt_encodes_candidate_priorities_and_tool_policy():
    prompt = AICandidateGenerator._system_prompt()

    assert "First request only get_cluster_overview and search_candidates" in prompt
    assert "prefer those values over the corresponding deployment_intent fields" in prompt
    assert "operator_preference as the primary recommendation criterion" in prompt
    assert "valid AIC predictions as secondary recommendation evidence" in prompt
    assert "Only when that search returns zero AIC candidates" in prompt
    assert "scale replicas first (prefill for TTFT, decode for TPOT in PD), then supported TP" in prompt
    assert "do not shrink below the anchor or exceed GPU/VRAM limits" in prompt
    assert "EP" not in prompt
    assert "Except for AIC-grounded scale-up proposals under rule 3" in prompt
    assert "unverified, not an AIC prediction or SLO guarantee" in prompt
    assert "deployment_intent.use_case and its resolved workload signals" in prompt
    assert "code-generation or a high shared_prefix_ratio" not in prompt
    assert "prefill_heavy workloads, prefer pd-disaggregation" not in prompt
    assert "historical" not in prompt
    assert "materially diverse candidates" in prompt
    assert "each feasible provider_ref" in prompt
    assert "GPU memory pressure" in prompt
    assert "CPU capacity" in prompt
    assert "minimum-resource topology" in prompt
    assert "lowest valid tensor parallelism and one replica per role" in prompt
    assert "If the initial search is empty, use the relaxed anchor" in prompt
    assert "operator_preference may select a provider but does not justify additional resources by itself" in prompt
    assert "Relaxed-anchor scaling remains unverified" in prompt
    assert "baseline-vllm: direct standard vLLM deployment" in prompt
    assert "optimized-baseline: vLLM with llm-d prefix-affinity" in prompt
    assert "pd-disaggregation: separate prefill and decode roles" in prompt
    assert "tiered-prefix-cache: vLLM with tiered prefix caching that uses available CPU buffer" in prompt
    assert "to relieve GPU VRAM pressure" in prompt
    assert "Use tiered-prefix-cache only when the reported CPU buffer meets required_cpu_buffer_gib" in prompt
    assert "only the provided allowlisted MCP tools" in prompt


def test_ai_generator_emits_sanitized_progress_events(monkeypatch):
    events = []

    class Tools:
        async def available_tools(self):
            return [_cluster_tool(), _aic_tool()]

        async def call(self, name, _arguments):
            return _cluster_result() if name == "get_cluster_overview" else {"candidates": []}

    class Provider:
        turns = 0

        async def complete_tool_turn(self, **_kwargs):
            self.turns += 1
            if self.turns == 1:
                return AIToolTurn(
                    content="private reasoning",
                    tool_calls=[
                        AIToolCall(id="cluster", name="get_cluster_overview", arguments={"clusterId": "cluster-a"}),
                        AIToolCall(id="aic", name="search_candidates", arguments=_aic_arguments()),
                    ],
                )
            return AIToolTurn(
                tool_calls=[
                    AIToolCall(
                        id="submit",
                        name="submit_candidate_proposals",
                        arguments={
                            "candidates": [
                                {
                                    "provider_ref": "optimized-baseline",
                                    "replicas": 1,
                                    "tensor_parallel_size": 1,
                                    "rationale": "Small valid candidate.",
                                }
                            ]
                        },
                    )
                ]
            )

    async def on_progress(event):
        events.append(event)

    monkeypatch.setattr("llm_d_bench.agentic.generator.client_for", lambda **_kwargs: Provider())
    generator = AICandidateGenerator(
        OpenAIPlannerSettings(base_url="http://provider", model="planner"),
        tools=Tools(),
    )

    asyncio.run(
        generator.generate(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(8, 32, 4),
            None,
            on_progress=on_progress,
        )
    )

    assert [event["phase"] for event in events] == [
        "ai",
        "ai",
        "tools",
        "tools",
        "tools",
        "ai",
        "generation",
    ]
    assert events[1]["message"] == "AI requested evidence from get_cluster_overview, search_candidates."
    assert {events[3]["tool"], events[4]["tool"]} == {"get_cluster_overview", "search_candidates"}
    assert all("private reasoning" not in event["message"] for event in events)
    assert all("arguments" not in event for event in events)
