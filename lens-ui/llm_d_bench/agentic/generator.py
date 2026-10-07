"""Candidate generation contracts and deterministic validation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from llm_d_bench.ai_providers.client import AIProviderClientError, AIToolCall, client_for
from llm_d_bench.auth.security import sign_internal
from llm_d_bench.capacity import (
    KVCacheDetail,
    allocatable_kv_cache_memory,
    find_possible_tp,
    load_model_config,
    max_concurrent_requests,
    wrap_config,
)

from .facts import PlanningEvidence, ResolvedPlanningFacts, _aic_predictions, _select_gpu_capacity
from .planner import OpenAIPlannerSettings, PlannedCandidate, PlanningFacts, WorkloadProfile, provider_guidance

_GENERATOR_MAX_TOKENS = 8192
_MCP_TIMEOUT_SECONDS = 300
_MAX_TOOL_ROUNDS = 4
_MAX_TOOL_CALLS = 8
_MAX_CALLS_PER_ROUND = 3
_MAX_TOOL_RESULT_BYTES = 200_000
_BASE_MCP_TOOLS = frozenset({"get_cluster_overview", "search_candidates"})
_RELAXED_AIC_TOOL = "search_aic_with_relaxation"
_OPTIONAL_MCP_TOOLS = frozenset(
    {
        "estimate_vllm_capacity",
        "get_configuration_capabilities",
        "get_evaluate_run",
        "get_simulation_task",
        "list_agentic_plans",
        "list_configuration_artifacts",
        "list_deployment_executions",
        "list_evaluate_runs",
        "list_evaluation_workflows",
        "list_model_cache_entries",
        "list_simulation_tasks",
    }
)
_ALLOWED_MCP_TOOLS = _BASE_MCP_TOOLS | _OPTIONAL_MCP_TOOLS | {_RELAXED_AIC_TOOL}
logger = logging.getLogger(__name__)


ProviderRef = Literal[
    "baseline-vllm",
    "optimized-baseline",
    "pd-disaggregation",
    "tiered-prefix-cache",
    "precise-prefix-cache-routing",
]
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class CandidateProposal(BaseModel):
    """A bounded topology proposed by an AI generator before validation."""

    model_config = ConfigDict(extra="forbid")

    provider_ref: ProviderRef
    replicas: int = Field(ge=1, le=128)
    tensor_parallel_size: int = Field(ge=1, le=128)
    prefill_replicas: int | None = Field(default=None, ge=1, le=128)
    prefill_tensor_parallel_size: int | None = Field(default=None, ge=1, le=128)
    rationale: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def normalize_pd_decode_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("provider_ref") != "pd-disaggregation":
            return value
        normalized = dict(value)
        for alias, field in (
            ("decode_replicas", "replicas"),
            ("decode_tensor_parallel_size", "tensor_parallel_size"),
        ):
            if field not in normalized and alias in normalized:
                normalized[field] = normalized[alias]
            normalized.pop(alias, None)
        return normalized


class CandidateProposalSet(BaseModel):
    """Strict structured response accepted from the AI generator."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateProposal] = Field(min_length=1, max_length=10)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)


class CandidateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateProposal] = Field(min_length=1, max_length=10)


class CandidateGenerationResult(BaseModel):
    proposals: CandidateProposalSet
    resolved: ResolvedPlanningFacts


class CandidateValidationResult(BaseModel):
    accepted: list[PlannedCandidate] = Field(default_factory=list)
    rejected: list[PlannedCandidate] = Field(default_factory=list)


class CandidateGenerationError(RuntimeError):
    """The external provider or required MCP planning tools were unavailable."""


class PlanningToolClient(Protocol):
    async def available_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> Any: ...


class AgenticMcpClient:
    """Read bounded cluster and AIC context from Prism's existing MCP endpoint."""

    def __init__(self, url: str | None = None, *, principal_id: str | None = None) -> None:
        self.url = url or os.getenv("PRISM_MCP_URL", "http://127.0.0.1:3000/api/mcp")
        self.principal_id = principal_id

    def _headers(self) -> dict[str, str]:
        secret = os.getenv("LENS_INTERNAL_AUTH_SECRET", "")
        if not secret or not self.principal_id:
            return {}
        timestamp = int(time.time())
        path = urlparse(self.url).path or "/"
        return {
            "x-prism-principal-id": self.principal_id,
            "x-prism-internal-ts": str(timestamp),
            "x-prism-internal-sig": sign_internal(
                secret,
                timestamp=timestamp,
                method="POST",
                path=path,
                principal_id=self.principal_id,
            ),
        }

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        ca_file = os.getenv("PRISM_MCP_CA_FILE")
        verify: bool | ssl.SSLContext = ssl.create_default_context(cafile=ca_file) if ca_file else True
        async with (
            httpx.AsyncClient(
                verify=verify,
                timeout=_MCP_TIMEOUT_SECONDS,
                headers=self._headers(),
            ) as http_client,
            streamable_http_client(
                self.url,
                http_client=http_client,
                terminate_on_close=False,
            ) as (read_stream, write_stream, _get_session_id),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=_MCP_TIMEOUT_SECONDS),
            ) as session,
        ):
            await session.initialize()
            yield session

    async def available_tools(self) -> list[dict[str, Any]]:
        try:
            async with self._session() as session:
                catalog = await session.list_tools()
        except Exception as error:
            raise CandidateGenerationError(f"MCP tool catalog failed: {error}") from error
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in catalog.tools
            if tool.name in _ALLOWED_MCP_TOOLS
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in _ALLOWED_MCP_TOOLS:
            raise CandidateGenerationError(f'MCP tool "{name}" is not allowed for AI-directed planning')
        try:
            async with self._session() as session:
                return await self._call(session, name, arguments)
        except CandidateGenerationError:
            raise
        except Exception as error:
            raise CandidateGenerationError(f'MCP tool "{name}" failed: {error}') from error

    @staticmethod
    async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
        if name not in _ALLOWED_MCP_TOOLS:
            raise CandidateGenerationError(f'MCP tool "{name}" is not allowed for candidate generation')
        try:
            result = await session.call_tool(name, arguments)
        except Exception as error:
            raise CandidateGenerationError(f'MCP tool "{name}" failed: {error}') from error
        if result.isError:
            raise CandidateGenerationError(f'MCP tool "{name}" returned an error')
        if result.structuredContent is not None:
            return result.structuredContent
        text = "".join(getattr(item, "text", "") for item in result.content)
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise CandidateGenerationError(f'MCP tool "{name}" returned invalid JSON') from error


class AICandidateGenerator:
    """Use MCP planning evidence and an external model to propose bounded candidates."""

    def __init__(
        self,
        settings: OpenAIPlannerSettings,
        tools: PlanningToolClient | None = None,
        *,
        principal_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.tools = tools or AgenticMcpClient(principal_id=principal_id)

    async def generate(
        self,
        cluster_id: str,
        model: str,
        facts: PlanningFacts,
        operator_prompt: str | None,
        on_progress: ProgressCallback | None = None,
    ) -> CandidateGenerationResult:
        try:
            facts = self._apply_preference_constraints(facts, operator_prompt)
            available_tools = await self.tools.available_tools()
            available_names = {tool["name"] for tool in available_tools}
            base_tools = set(_BASE_MCP_TOOLS)
            missing_base_tools = base_tools - available_names
            if missing_base_tools:
                missing = ", ".join(sorted(missing_base_tools))
                raise CandidateGenerationError(f"Required MCP tools are unavailable: {missing}")
            client = client_for(
                base_url=str(self.settings.base_url),
                model=self.settings.model,
                api_key=self.settings.api_key,
                timeout_seconds=max(self.settings.timeout_seconds, 60),
                provider_type=self.settings.provider_type,
            )
            intent = asdict(facts)
            for field in ("vram_per_gpu_gib", "free_gpu_count", "cpu_buffer_gib", "aic_predictions", "evidence_ids"):
                intent.pop(field, None)
            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "cluster_id": cluster_id,
                            "model": model,
                            "operator_preference": operator_prompt or "",
                            "deployment_intent": intent,
                        }
                    ),
                }
            ]
            provider_tools = self._provider_tools(available_tools)
            total_calls = 0
            remaining_result_bytes = _MAX_TOOL_RESULT_BYTES
            empty_tool_retries = 0
            invalid_submission_retries = 0
            initial_tool_retries = 0
            tool_trace: list[dict[str, Any]] = []
            evidence_results: dict[str, list[Any]] = {}
            attempted_tools: set[str] = set()
            for _round in range(_MAX_TOOL_ROUNDS + 1):
                initial_search = evidence_results.get("search_candidates", [])
                needs_relax = (
                    _RELAXED_AIC_TOOL in available_names
                    and bool(initial_search)
                    and isinstance(initial_search[-1], dict)
                    and initial_search[-1].get("candidates") == []
                )
                can_relax = needs_relax and _RELAXED_AIC_TOOL not in attempted_tools
                turn_tools = (
                    [
                        tool
                        for tool in provider_tools
                        if can_relax or self._provider_tool_name(tool) != _RELAXED_AIC_TOOL
                    ]
                    if attempted_tools
                    else [tool for tool in provider_tools if self._provider_tool_name(tool) in base_tools]
                )
                if _round == _MAX_TOOL_ROUNDS:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The MCP tool budget is complete. Submit the best supported candidates now; "
                                "do not request more evidence."
                            ),
                        }
                    )
                    turn_tools = [
                        tool
                        for tool in provider_tools
                        if self._provider_tool_name(tool) == "submit_candidate_proposals"
                    ]
                await _emit_progress(
                    on_progress,
                    {
                        "phase": "ai",
                        "status": "running",
                        "round": _round + 1,
                        "message": "Waiting for the AI generator response.",
                    },
                )
                turn = await client.complete_tool_turn(
                    system_prompt=self._system_prompt(),
                    messages=messages,
                    tools=turn_tools,
                    max_tokens=_GENERATOR_MAX_TOKENS,
                )
                submission = next((call for call in turn.tool_calls if call.name == "submit_candidate_proposals"), None)
                if submission is not None:
                    if len(turn.tool_calls) != 1:
                        raise CandidateGenerationError("AI candidate generation mixed submission with MCP calls")
                    if "get_cluster_overview" not in evidence_results:
                        raise CandidateGenerationError(
                            "AI candidate generation returned an invalid response: cluster evidence was not gathered"
                        )
                    if "search_candidates" not in attempted_tools:
                        raise CandidateGenerationError(
                            "AI candidate generation returned an invalid response: AIC evidence was not requested"
                        )
                    if needs_relax and _RELAXED_AIC_TOOL not in attempted_tools:
                        raise CandidateGenerationError(
                            "AI candidate generation skipped relaxed AIC search after an empty result"
                        )
                    try:
                        submitted = CandidateSubmission.model_validate(submission.arguments)
                    except ValidationError as error:
                        if invalid_submission_retries >= 1:
                            raise
                        invalid_submission_retries += 1
                        messages.append(
                            {
                                "role": "assistant",
                                "content": turn.content,
                                "tool_calls": [submission.model_dump()],
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "results": [
                                    {
                                        "tool_call_id": submission.id,
                                        "content": json.dumps(
                                            {
                                                "error": (
                                                    "submission schema invalid; call submit_candidate_proposals again "
                                                    "with a candidates array"
                                                ),
                                                "validation_errors": [item["loc"] for item in error.errors()],
                                            }
                                        ),
                                    }
                                ],
                            }
                        )
                        continue
                    await _emit_progress(
                        on_progress,
                        {
                            "phase": "generation",
                            "status": "complete",
                            "message": f"AI proposed {len(submitted.candidates)} candidate(s).",
                        },
                    )
                    candidates = list(submitted.candidates)
                    relaxed = (evidence_results.get(_RELAXED_AIC_TOOL) or [None])[-1]
                    anchor = relaxed.get("anchor") if isinstance(relaxed, dict) else None
                    if isinstance(anchor, dict) and anchor.get("source") == "aic":
                        mode = anchor.get("topologyMode")
                        try:
                            topology = {
                                "provider_ref": "pd-disaggregation" if mode == "disagg" else "baseline-vllm",
                                "replicas": anchor["decodeReplicas"],
                                "tensor_parallel_size": anchor["decodeTp"],
                                "rationale": "Validated relaxed AIC anchor; original latency SLO remains unverified.",
                            }
                            if mode == "disagg":
                                topology.update(
                                    prefill_replicas=anchor["prefillReplicas"],
                                    prefill_tensor_parallel_size=anchor["prefillTp"],
                                )
                            if mode in {"agg", "disagg"}:
                                anchor_candidate = CandidateProposal.model_validate(topology)
                                if not any(
                                    (
                                        candidate.provider_ref,
                                        candidate.replicas,
                                        candidate.tensor_parallel_size,
                                        candidate.prefill_replicas,
                                        candidate.prefill_tensor_parallel_size,
                                    )
                                    == (
                                        anchor_candidate.provider_ref,
                                        anchor_candidate.replicas,
                                        anchor_candidate.tensor_parallel_size,
                                        anchor_candidate.prefill_replicas,
                                        anchor_candidate.prefill_tensor_parallel_size,
                                    )
                                    for candidate in candidates
                                ):
                                    candidates = [*candidates[:9], anchor_candidate]
                        except (KeyError, ValidationError):
                            pass
                    proposals = CandidateProposalSet(candidates=candidates, tool_trace=tool_trace)
                    resolved = self._resolve_mcp_facts(cluster_id, facts, evidence_results)
                    return CandidateGenerationResult(proposals=proposals, resolved=resolved)
                if not turn.tool_calls:
                    if empty_tool_retries < 1:
                        empty_tool_retries += 1
                        if turn.content.strip():
                            messages.append({"role": "assistant", "content": turn.content})
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your response did not call a tool. Continue by calling one provided tool; "
                                    "do not reply with text only."
                                ),
                            }
                        )
                        continue
                    raise CandidateGenerationError("AI candidate generation returned no tool calls or submission")
                if _round >= _MAX_TOOL_ROUNDS:
                    raise CandidateGenerationError(
                        f"AI candidate generation exceeded the tool-round limit: "
                        f"round {_round + 1} (limit {_MAX_TOOL_ROUNDS})"
                    )
                if len(turn.tool_calls) > _MAX_CALLS_PER_ROUND:
                    raise CandidateGenerationError(
                        f"AI candidate generation returned too many tool calls in round {_round + 1}: "
                        f"{len(turn.tool_calls)} (limit {_MAX_CALLS_PER_ROUND})"
                    )
                if total_calls + len(turn.tool_calls) > _MAX_TOOL_CALLS:
                    raise CandidateGenerationError(
                        f"AI candidate generation returned too many tool calls: "
                        f"{total_calls + len(turn.tool_calls)} (limit {_MAX_TOOL_CALLS})"
                    )
                if remaining_result_bytes <= 0:
                    raise CandidateGenerationError("AI candidate generation exhausted the tool-result budget")
                per_call_budget = max(1, remaining_result_bytes // len(turn.tool_calls))
                turn.tool_calls = self._normalize_required_tool_arguments(turn.tool_calls, cluster_id, model, facts)
                if any(call.name == _RELAXED_AIC_TOOL for call in turn.tool_calls) and not can_relax:
                    raise CandidateGenerationError("Relaxed AIC search requires an empty initial AIC result")
                try:
                    self._validate_tool_calls(turn.tool_calls, cluster_id, initial_round=not attempted_tools)
                except CandidateGenerationError as error:
                    if attempted_tools or initial_tool_retries >= 1:
                        raise
                    initial_tool_retries += 1
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your initial tool calls were rejected: {error}. "
                            "Retry once by calling exactly get_cluster_overview and search_candidates "
                            "together, and no other tools."
                        ),
                    })
                    await _emit_progress(on_progress, {
                        "phase": "ai", "status": "running", "round": _round + 1,
                        "message": "The first AI tool batch was invalid; retrying with the required tool pair.",
                    })
                    continue
                attempted_tools.update(call.name for call in turn.tool_calls)
                await _emit_progress(
                    on_progress,
                    {
                        "phase": "ai",
                        "status": "complete",
                        "round": _round + 1,
                        "message": "AI requested evidence from "
                        + ", ".join(call.name for call in turn.tool_calls)
                        + ".",
                    },
                )
                await _emit_progress(
                    on_progress,
                    {
                        "phase": "tools",
                        "status": "running",
                        "round": _round + 1,
                        "tools": [call.name for call in turn.tool_calls],
                        "message": "Running " + ", ".join(call.name for call in turn.tool_calls) + ".",
                    },
                )
                cluster_calls = [call for call in turn.tool_calls if call.name == "get_cluster_overview"]
                cluster_executions = await asyncio.gather(
                    *(self._execute_optional_tool(call, per_call_budget) for call in cluster_calls)
                )
                cluster_result = (evidence_results.get("get_cluster_overview") or [None])[-1]
                if cluster_executions:
                    cluster_result = cluster_executions[-1][0].get("result")
                other_calls = []
                for call in turn.tool_calls:
                    if call.name == "get_cluster_overview":
                        continue
                    if call.name == "search_candidates":
                        if cluster_result is None:
                            raise CandidateGenerationError("MCP cluster overview is required before AIC search")
                        budget = self._resolve_mcp_facts(
                            cluster_id,
                            facts,
                            {"get_cluster_overview": [cluster_result]},
                        ).facts.free_gpu_count
                        if budget < 1:
                            raise CandidateGenerationError(
                                "No GPUs available for AIC search in the selected capacity mode"
                            )
                        arguments = dict(call.arguments)
                        arguments["searchConfig"] = {**arguments.get("searchConfig", {}), "totalGpus": budget}
                        call = call.model_copy(update={"arguments": arguments})
                    other_calls.append(call)
                other_executions = await asyncio.gather(
                    *(self._execute_optional_tool(call, per_call_budget) for call in other_calls)
                )
                executed = {
                    call.id: (call, execution)
                    for call, execution in [
                        *zip(cluster_calls, cluster_executions, strict=False),
                        *zip(other_calls, other_executions, strict=False),
                    ]
                }
                turn.tool_calls = [executed[call.id][0] for call in turn.tool_calls]
                executions = [executed[call.id][1] for call in turn.tool_calls]
                results = [execution[0] for execution in executions]
                tool_trace.extend(execution[1] for execution in executions)
                for call, execution in zip(turn.tool_calls, executions, strict=True):
                    await _emit_progress(
                        on_progress,
                        {
                            "phase": "tools",
                            "status": execution[1]["status"],
                            "round": _round + 1,
                            "tool": call.name,
                            "duration_ms": execution[1]["duration_ms"],
                            "message": f"{call.name} {execution[1]['status']}.",
                        },
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content,
                        "tool_calls": [call.model_dump() for call in turn.tool_calls],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "results": [
                            {
                                "tool_call_id": call.id,
                                "content": json.dumps(result),
                            }
                            for call, result in zip(turn.tool_calls, results, strict=True)
                        ],
                    }
                )
                for call, result in zip(turn.tool_calls, results, strict=True):
                    if "result" in result:
                        evidence_results.setdefault(call.name, []).append(result["result"])
                total_calls += len(turn.tool_calls)
                remaining_result_bytes -= sum(len(json.dumps(result).encode("utf-8")) for result in results)
            raise CandidateGenerationError("AI candidate generation exceeded the tool-round limit")
        except CandidateGenerationError:
            raise
        except ValidationError as error:
            raise CandidateGenerationError(f"AI candidate generation returned an invalid response: {error}") from error
        except (AIProviderClientError, TypeError, ValueError) as error:
            raise CandidateGenerationError(f"AI candidate generation failed: {error}") from error

    async def _execute_optional_tool(
        self,
        call: AIToolCall,
        result_budget: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if call.name not in _ALLOWED_MCP_TOOLS:
            raise CandidateGenerationError(f'MCP tool "{call.name}" is not allowed for AI-directed planning')
        started = time.monotonic()
        arguments_json = json.dumps(call.arguments, sort_keys=True)
        try:
            result = await self.tools.call(call.name, call.arguments)
        except CandidateGenerationError as error:
            logger.warning("Agentic generator tool=%s status=error reason=%s", call.name, error)
            context_result = {"tool": call.name, "arguments": call.arguments, "error": str(error)}
            return context_result, self._tool_trace(call.name, arguments_json, str(error), started, "error")
        if call.name == "search_candidates" and isinstance(result, dict):
            detail = result.get("detail")
            if (
                result.get("error") == "upstream request failed (status 400)"
                and isinstance(detail, dict)
                and detail.get("error") == "No feasible configurations found for the given parameters."
            ):
                result = {**result, "candidates": []}
        encoded = json.dumps(result)
        if len(encoded.encode("utf-8")) > result_budget:
            result = {"truncated": True, "preview": encoded.encode("utf-8")[:result_budget].decode("utf-8", "ignore")}
        context_result = {"tool": call.name, "arguments": call.arguments, "result": result}
        logger.info("Agentic generator tool=%s status=success result_bytes=%d", call.name, len(encoded.encode("utf-8")))
        return context_result, self._tool_trace(call.name, arguments_json, encoded, started, "success")

    def _provider_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        definitions = [
            *tools,
            {
                "name": "submit_candidate_proposals",
                "description": "Submit the final deployment candidates after gathering sufficient MCP evidence.",
                "input_schema": CandidateSubmission.model_json_schema(),
            },
        ]
        if self.settings.provider_type == "anthropic":
            return definitions
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in definitions
        ]

    @staticmethod
    def _provider_tool_name(tool: dict[str, Any]) -> str | None:
        function = tool.get("function")
        return function.get("name") if isinstance(function, dict) else tool.get("name")

    @staticmethod
    def _apply_preference_constraints(facts: PlanningFacts, preference: str | None) -> PlanningFacts:
        if not preference:
            return facts
        values = {
            field.lower(): int(amount)
            for field, amount in re.findall(
                r"\b(ISL|OSL|TTFT|TPOT)\b\s*(?:of\s*)?"
                r"(?:(?:less than|under|below|<=|<|≤|=|:)\s*)?(\d+)\s*(?:ms|tokens?)?\b",
                preference,
                re.IGNORECASE,
            )
            if 0 < int(amount) <= 1_000_000
        }
        concurrency = re.search(r"\b(\d+)\s+concurrent\s+(?:users?|requests?)\b", preference, re.IGNORECASE)
        profile_updates: dict[str, int] = {}
        if "isl" in values:
            profile_updates.update(mean_input_tokens=values["isl"], p95_input_tokens=values["isl"])
        if "osl" in values:
            profile_updates["mean_output_tokens"] = values["osl"]
        if concurrency and 0 < int(concurrency.group(1)) <= 1_000_000:
            profile_updates["concurrency"] = int(concurrency.group(1))
        profile = facts.workload_profile
        if isinstance(profile, dict) and (profile_updates or values):
            profile = WorkloadProfile(**profile)
        if profile_updates:
            profile = replace(profile or WorkloadProfile(), **profile_updates)
        return replace(
            facts,
            workload_profile=profile,
            ttft_slo_ms=values.get("ttft", facts.ttft_slo_ms),
            tpot_slo_ms=values.get("tpot", facts.tpot_slo_ms),
        )

    @staticmethod
    def _normalize_required_tool_arguments(
        calls: list[AIToolCall],
        cluster_id: str,
        model: str,
        facts: PlanningFacts,
    ) -> list[AIToolCall]:
        normalized: list[AIToolCall] = []
        for call in calls:
            arguments = dict(call.arguments)
            if call.name == "get_cluster_overview":
                arguments["clusterId"] = cluster_id
            elif call.name in {"search_candidates", _RELAXED_AIC_TOOL}:
                if call.name == "search_candidates":
                    arguments["sourceIds"] = ["aic"]
                else:
                    arguments["clusterId"] = cluster_id
                workload = dict(arguments.get("workload") or {})
                workload["model"] = model
                profile = facts.workload_profile
                input_tokens = (
                    profile.get("mean_input_tokens")
                    if isinstance(profile, dict)
                    else profile.mean_input_tokens
                    if profile
                    else None
                )
                output_tokens = (
                    profile.get("mean_output_tokens")
                    if isinstance(profile, dict)
                    else profile.mean_output_tokens
                    if profile
                    else None
                )
                workload["isl"] = int(input_tokens) if input_tokens is not None else 1024
                workload["osl"] = int(output_tokens) if output_tokens is not None else 256
                for target, value in (("ttftMs", facts.ttft_slo_ms), ("tpotMs", facts.tpot_slo_ms)):
                    if value is None:
                        workload.pop(target, None)
                    else:
                        workload[target] = value
                arguments["workload"] = workload
                if call.name == _RELAXED_AIC_TOOL:
                    config = dict(arguments.get("searchConfig") or {})
                    config.pop("totalGpus", None)
                    config["capacityMode"] = facts.hardware_capacity_mode
                    config["customGpuCount"] = facts.custom_gpu_count
                    arguments["searchConfig"] = config
            normalized.append(call.model_copy(update={"arguments": arguments}))
        return normalized

    @staticmethod
    def _validate_tool_calls(
        calls: list[AIToolCall],
        cluster_id: str,
        *,
        initial_round: bool = False,
        aic_tool: str = "search_candidates",
    ) -> None:
        for call in calls:
            if call.name not in _ALLOWED_MCP_TOOLS:
                raise CandidateGenerationError(f'MCP tool "{call.name}" is not allowed for AI-directed planning')
            if call.name == "get_cluster_overview" and call.arguments.get("clusterId") != cluster_id:
                raise CandidateGenerationError("AI requested cluster evidence for the wrong cluster")
            if call.name == "search_candidates" and "aic" not in (call.arguments.get("sourceIds") or []):
                raise CandidateGenerationError("AI candidate search must include the AIC source")
        if initial_round and (len(calls) != 2 or {call.name for call in calls} != {"get_cluster_overview", aic_tool}):
            raise CandidateGenerationError(
                "AI candidate generation must first call only get_cluster_overview and AIC search_candidates"
            )

    @staticmethod
    def _resolve_mcp_facts(
        cluster_id: str,
        requested: PlanningFacts,
        results: dict[str, list[Any]],
    ) -> ResolvedPlanningFacts:
        cluster = results["get_cluster_overview"][-1]
        hardware = cluster.get("hardware") if isinstance(cluster, dict) else None
        if not isinstance(hardware, dict) and isinstance(cluster, dict):
            kubernetes = cluster.get("kubernetes")
            hardware = kubernetes.get("hardware") if isinstance(kubernetes, dict) else None
        if not isinstance(hardware, dict):
            raise CandidateGenerationError("MCP cluster overview has no hardware facts")
        gpu_count = int(hardware.get("gpuCount") or 0)
        available_gpus = int(hardware.get("availableGpuCount") or 0)
        vram_bytes = int(hardware.get("vramBytes") or 0)
        if gpu_count < 1 or vram_bytes < 1:
            raise CandidateGenerationError("MCP cluster overview has no usable accelerator VRAM")
        free_gpu_count = _select_gpu_capacity(requested, available_gpus, gpu_count)
        memory_bytes = int(hardware.get("memoryBytes") or 0)
        memory_usage = hardware.get("memoryUsagePercent")
        cpu_buffer_gib = requested.cpu_buffer_gib
        if memory_bytes and isinstance(memory_usage, (int, float)):
            cpu_buffer_gib = max(0.0, memory_bytes * (1 - memory_usage / 100) / (1024**3))
        aic_results = results.get("search_candidates", [])
        if aic_results and isinstance(aic_results[-1], dict) and aic_results[-1].get("candidates") == []:
            aic_results = results.get(_RELAXED_AIC_TOOL, []) or aic_results
        raw_predictions = _aic_predictions_from_mcp(aic_results[-1]) if aic_results else []
        predictions = tuple(
            prediction for prediction in raw_predictions if _aic_prediction_satisfies_slo(requested, prediction)
        )
        evidence = [
            PlanningEvidence(
                id=f"cluster-overview:{cluster_id}",
                source="cluster-overview",
                status="available",
                details={"cluster_id": cluster_id, "available_gpu_count": available_gpus},
            )
        ]
        if predictions:
            evidence.append(
                PlanningEvidence(
                    id="aic:search",
                    source="aic",
                    status="available",
                    details={"candidates": aic_results[-1]},
                )
            )
        resolved = replace(
            requested,
            vram_per_gpu_gib=vram_bytes / gpu_count / (1024**3),
            free_gpu_count=free_gpu_count,
            cpu_buffer_gib=cpu_buffer_gib,
            aic_predictions=predictions,
            evidence_ids=tuple(item.id for item in evidence),
        )
        profile = requested.workload_profile
        return ResolvedPlanningFacts(
            facts=resolved,
            cluster_snapshot=cluster,
            evidence=evidence,
            trace=[
                {
                    "phase": "workload",
                    "status": "complete",
                    "profile": asdict(profile) if profile and not isinstance(profile, dict) else profile,
                    "ttft_slo_ms": requested.ttft_slo_ms,
                    "tpot_slo_ms": requested.tpot_slo_ms,
                    "context_length": requested.context_length,
                },
                {
                    "phase": "facts",
                    "status": "complete",
                    "source": "mcp",
                    "available_gpu_count": available_gpus,
                    "selected_gpu_count": free_gpu_count,
                    "vram_per_gpu_gib": resolved.vram_per_gpu_gib,
                    "cpu_buffer_gib": cpu_buffer_gib,
                    "aic_prediction_count": len(raw_predictions),
                    "aic_predictions_retained": len(predictions),
                    "aic_predictions_filtered_by_slo": len(raw_predictions) - len(predictions),
                },
            ],
        )

    @staticmethod
    def _tool_trace(
        name: str,
        arguments_json: str,
        result_text: str,
        started: float,
        status: str,
    ) -> dict[str, Any]:
        trace = {
            "tool": name,
            "arguments_digest": hashlib.sha256(arguments_json.encode()).hexdigest(),
            "result_digest": hashlib.sha256(result_text.encode()).hexdigest(),
            "duration_ms": round((time.monotonic() - started) * 1_000),
            "status": status,
        }
        if name in {"search_candidates", _RELAXED_AIC_TOOL}:
            trace["arguments"] = json.loads(arguments_json)
            try:
                trace["result"] = json.loads(result_text)
            except json.JSONDecodeError:
                trace["result"] = result_text
        return trace

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are Prism's candidate generator. Use the provided tools to gather evidence, then submit candidates.\n"
            "Generate 1 to 10 materially useful deployment candidates using only these provider_ref values: "
            "baseline-vllm, optimized-baseline, pd-disaggregation, tiered-prefix-cache, "
            "precise-prefix-cache-routing.\n"
            "Provider semantics:\n"
            f"{provider_guidance()}\n"
            "Mandatory constraints:\n"
            "1. Treat deployment_intent as operator input and MCP tool results as "
            "runtime facts; never invent hardware, "
            "providers, guide variants, AIC predictions, or benchmark evidence.\n"
            "2. First request only get_cluster_overview and search_candidates (sourceIds: [aic]); the server runs the "
            "cluster overview first and fixes the AIC GPU budget to the selected live capacity. Only when that search "
            "returns zero AIC candidates, call search_aic_with_relaxation if available. Before proposing candidates, "
            "calculate concrete numeric limits for available GPUs, per-GPU VRAM, CPU capacity and buffer, context "
            "length, and every explicit SLO from the deployment intent and these results.\n"
            "If operator_preference states numeric ISL, OSL, TTFT or TPOT limits, prefer those values over the "
            "corresponding deployment_intent fields when requesting AIC predictions and evaluating SLOs.\n"
            "3. Cluster resource constraints are hard constraints; Prefer AIC results meeting the original SLOs. "
            "If the initial search is empty, use the relaxed anchor to scale replicas first (prefill for TTFT, "
            "decode for TPOT "
            "in PD), then supported TP. Keep the mode, do not shrink below the anchor or exceed GPU/VRAM limits. "
            "Cite the anchor; scaling is unverified, not an AIC prediction or SLO guarantee. No anchor means no "
            "AIC-grounded estimate.\n"
            "4. After hard constraints, use operator_preference as the primary recommendation criterion. Use valid AIC "
            "predictions as secondary recommendation evidence only for aggregated and disaggregated topologies. AIC "
            "does not support other guide types: missing AIC results for those guides are not applicable, not negative "
            "evidence, so never discard, demote, or penalize them for lacking AIC predictions.\n"
            "5. Treat deployment_intent.use_case and its resolved workload signals as important "
            "scenario-specific guidance.\n"
            "6. Resource use includes GPU count, GPU memory pressure, and CPU capacity. Except for "
            "AIC-grounded scale-up proposals under rule 3, for every feasible provider first propose its "
            "minimum-resource topology: the lowest "
            "valid tensor parallelism and one replica per role. Increase tensor parallelism or replicas only when an "
            "explicit SLO or complete AIC prediction supports additional resources; operator_preference may select a "
            "provider but does not justify additional resources by itself. Relaxed-anchor scaling remains unverified.\n"
            "7. Before submitting, enumerate feasible provider choices. Produce materially diverse "
            "candidates: include at "
            "least one candidate for each feasible provider_ref. Omit a provider only when a reported hard resource "
            "constraint or explicit SLO makes it infeasible; avoid token diversity.\n"
            "8. required_gpus is replicas * tensor_parallel_size for single-role candidates. For PD it is the sum of "
            "prefill_replicas * prefill_tensor_parallel_size and replicas * tensor_parallel_size; replicas and "
            "tensor_parallel_size are the decode-role fields, so never return separate decode_* fields.\n"
            "9. PD candidates must provide both prefill fields. Non-PD candidates must omit both prefill fields.\n"
            "10. Use tiered-prefix-cache only when the reported CPU buffer meets required_cpu_buffer_gib.\n"
            "11. Remove duplicate topologies. Do not score, select, render, approve, or deploy candidates.\n"
            "12. Cite cluster-overview:<cluster_id> only after that MCP call succeeded. Cite aic:search only when AIC "
            "returned a complete prediction that satisfies every explicit TTFT and TPOT SLO. The server will "
            "independently recompute all resource constraints and reject invalid proposals.\n"
            "Tool policy:\n"
            "13. Before submitting, call get_cluster_overview for the requested cluster and search_candidates with "
            "sourceIds including aic; use search_aic_with_relaxation only after an empty result.\n"
            "14. Use only the provided allowlisted MCP tools, never call write, deploy, approve, delete, "
            "or port-forward "
            "operations, and do not repeat equivalent calls.\n"
            "15. Finish by calling submit_candidate_proposals exactly once with one to ten candidates."
        )


async def _emit_progress(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback is not None:
        await callback(event)


def _aic_predictions_from_mcp(result: Any) -> list[Any]:
    candidates = result.get("candidates", []) if isinstance(result, dict) else []
    raw_configs: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("source") != "aic":
            continue
        predicted = candidate.get("predicted") or {}
        if candidate.get("topologyMode") == "agg":
            raw_configs.append(
                {
                    "mode": "agg",
                    "tp": candidate.get("decodeTp"),
                    "replicas": candidate.get("decodeReplicas"),
                    "ttft_ms": predicted.get("ttftMs"),
                    "tpot_ms": predicted.get("tpotMs"),
                    "throughput_tokens_per_sec": predicted.get("throughputTps"),
                }
            )
        elif candidate.get("topologyMode") == "disagg":
            raw_configs.append(
                {
                    "mode": "disagg",
                    "prefill_tp": candidate.get("prefillTp"),
                    "prefill_replicas": candidate.get("prefillReplicas"),
                    "decode_tp": candidate.get("decodeTp"),
                    "decode_replicas": candidate.get("decodeReplicas"),
                    "ttft_ms": predicted.get("ttftMs"),
                    "tpot_ms": predicted.get("tpotMs"),
                    "throughput_tokens_per_sec": predicted.get("throughputTps"),
                }
            )
    return _aic_predictions(raw_configs)


def _aic_prediction_satisfies_slo(requested: PlanningFacts, prediction: Any) -> bool:
    return (
        any((prediction.ttft_ms, prediction.tpot_ms, prediction.throughput_tokens_per_sec))
        and (
            requested.ttft_slo_ms is None
            or (prediction.ttft_ms is not None and prediction.ttft_ms <= requested.ttft_slo_ms)
        )
        and (
            requested.tpot_slo_ms is None
            or (prediction.tpot_ms is not None and prediction.tpot_ms <= requested.tpot_slo_ms)
        )
    )


class CandidateValidator:
    """Normalize AI proposals and independently enforce deployment constraints."""

    def validate(self, facts: PlanningFacts, proposals: list[CandidateProposal]) -> CandidateValidationResult:
        accepted: list[PlannedCandidate] = []
        rejected: list[PlannedCandidate] = []
        seen: set[tuple[object, ...]] = set()

        model_config = None
        if facts.model_config_dict is not None:
            model_config = wrap_config(facts.model_config_dict)
        elif facts.model_name:
            model_config = load_model_config(facts.model_name)

        for proposal in proposals:
            topology = (
                proposal.provider_ref,
                proposal.replicas,
                proposal.tensor_parallel_size,
                proposal.prefill_replicas,
                proposal.prefill_tensor_parallel_size,
            )
            if topology in seen:
                continue
            seen.add(topology)

            reasons = self._rejection_reasons(facts, proposal, model_config=model_config)
            required_gpus = self._required_gpus(proposal)

            allocatable_kv_gib: float | None = None
            per_request_kv_gib: float | None = None
            concurrency: int | None = None

            avail_kv = allocatable_kv_cache_memory(
                facts.model_name or "model",
                model_config,
                gpu_memory=facts.vram_per_gpu_gib,
                gpu_util=0.9,
                tp=proposal.tensor_parallel_size,
                pp=1,
                dp=1,
                max_model_len=facts.context_length,
                fallback_weight_gib=facts.model_weight_gib,
            )
            kv_detail = KVCacheDetail(
                facts.model_name or "model",
                model_config,
                context_len=facts.context_length,
                batch_size=1,
            )
            per_req_kv = kv_detail.per_request_kv_cache_gb
            concurrency = max_concurrent_requests(
                facts.model_name or "model",
                model_config,
                max_model_len=facts.context_length,
                gpu_memory=facts.vram_per_gpu_gib,
                gpu_util=0.9,
                tp=proposal.tensor_parallel_size,
                pp=1,
                dp=1,
                fallback_weight_gib=facts.model_weight_gib,
            )
            allocatable_kv_gib = round(avail_kv, 3)
            per_request_kv_gib = round(per_req_kv, 3)

            if avail_kv < 0:
                reasons.append("insufficient GPU memory to load model weights and activation")
            elif avail_kv < per_req_kv:
                reasons.append("insufficient KV cache for context length")

            candidate = PlannedCandidate(
                id=self._candidate_id(proposal),
                provider_ref=proposal.provider_ref,
                replicas=proposal.replicas,
                tensor_parallel_size=proposal.tensor_parallel_size,
                prefill_replicas=proposal.prefill_replicas,
                prefill_tensor_parallel_size=proposal.prefill_tensor_parallel_size,
                guide_variant=self._guide_variant(proposal.provider_ref),
                max_model_len=facts.context_length,
                gpu_memory_utilization=0.9,
                required_gpus=required_gpus,
                deployable=not reasons,
                rejection_reasons=reasons,
                evidence_ids=list(
                    dict.fromkeys(
                        [
                            *facts.evidence_ids,
                            *(
                                evidence_id
                                for evidence_id in proposal.evidence_ids
                                if evidence_id in facts.evidence_ids
                            ),
                        ]
                    )
                ),
                vllm_arguments=[{"name": name, "value": value} for name, value in facts.vllm_arguments],
                allocatable_kv_cache_gib=allocatable_kv_gib,
                per_request_kv_cache_gib=per_request_kv_gib,
                max_concurrent_requests=concurrency,
            )
            (accepted if candidate.deployable else rejected).append(candidate)

        return CandidateValidationResult(accepted=accepted, rejected=rejected)

    @staticmethod
    def _required_gpus(proposal: CandidateProposal) -> int:
        decode_gpus = proposal.replicas * proposal.tensor_parallel_size
        if proposal.provider_ref != "pd-disaggregation":
            return decode_gpus
        if proposal.prefill_replicas is None or proposal.prefill_tensor_parallel_size is None:
            return decode_gpus
        return decode_gpus + proposal.prefill_replicas * proposal.prefill_tensor_parallel_size

    @classmethod
    def _rejection_reasons(
        cls, facts: PlanningFacts, proposal: CandidateProposal, model_config: Any | None = None
    ) -> list[str]:
        reasons: list[str] = []
        is_pd = proposal.provider_ref == "pd-disaggregation"
        has_prefill = proposal.prefill_replicas is not None or proposal.prefill_tensor_parallel_size is not None
        if is_pd and (proposal.prefill_replicas is None or proposal.prefill_tensor_parallel_size is None):
            reasons.append("PD candidates require both prefill replicas and tensor parallelism")
        if not is_pd and has_prefill:
            reasons.append("single-role candidates cannot define a prefill topology")

        if model_config is not None:
            try:
                valid_tps = find_possible_tp(model_config)
                if proposal.tensor_parallel_size not in valid_tps:
                    reasons.append(f"TP={proposal.tensor_parallel_size} is invalid for model architecture")
                if is_pd and proposal.prefill_tensor_parallel_size is not None and proposal.prefill_tensor_parallel_size not in valid_tps:
                    reasons.append(f"Prefill TP={proposal.prefill_tensor_parallel_size} is invalid for model architecture")
            except Exception:
                pass

            avail_kv = allocatable_kv_cache_memory(
                facts.model_name or "model",
                model_config,
                gpu_memory=facts.vram_per_gpu_gib,
                gpu_util=0.9,
                tp=proposal.tensor_parallel_size,
                pp=1,
                dp=1,
                max_model_len=facts.context_length,
                fallback_weight_gib=facts.model_weight_gib,
            )
            kv_detail = KVCacheDetail(
                facts.model_name or "model",
                model_config,
                context_len=facts.context_length,
                batch_size=1,
            )
            per_req_kv = kv_detail.per_request_kv_cache_gb
            if avail_kv < 0:
                reasons.append("insufficient GPU memory to load model weights and activation")
            elif avail_kv < per_req_kv:
                reasons.append("insufficient KV cache for context length")

            if is_pd and proposal.prefill_tensor_parallel_size is not None:
                prefill_avail_kv = allocatable_kv_cache_memory(
                    facts.model_name or "model",
                    model_config,
                    gpu_memory=facts.vram_per_gpu_gib,
                    gpu_util=0.9,
                    tp=proposal.prefill_tensor_parallel_size,
                    pp=1,
                    dp=1,
                    max_model_len=facts.context_length,
                    fallback_weight_gib=facts.model_weight_gib,
                )
                if prefill_avail_kv < 0:
                    reasons.append("insufficient GPU memory for prefill to load model weights and activation")
                elif prefill_avail_kv < per_req_kv:
                    reasons.append("insufficient KV cache for prefill context length")
        else:
            footprint_gib = facts.model_weight_gib * 1.2 + max(1.0, facts.context_length / 4096)
            if footprint_gib > facts.vram_per_gpu_gib * proposal.tensor_parallel_size * 0.9:
                reasons.append("estimated model footprint exceeds selected decode TP VRAM")
            if (
                is_pd
                and proposal.prefill_tensor_parallel_size is not None
                and footprint_gib > facts.vram_per_gpu_gib * proposal.prefill_tensor_parallel_size * 0.9
            ):
                reasons.append("estimated model footprint exceeds selected prefill TP VRAM")
        if cls._required_gpus(proposal) > facts.free_gpu_count:
            reasons.append("insufficient free accelerator cards")
        if proposal.provider_ref == "tiered-prefix-cache" and facts.cpu_buffer_gib < facts.required_cpu_buffer_gib:
            reasons.append("insufficient CPU buffer for tiered prefix cache")
        return reasons

    @staticmethod
    def _candidate_id(proposal: CandidateProposal) -> str:
        if proposal.provider_ref == "pd-disaggregation":
            return (
                f"pd-disaggregation-p{proposal.prefill_replicas}-tp{proposal.prefill_tensor_parallel_size}"
                f"-d{proposal.replicas}-tp{proposal.tensor_parallel_size}"
            )
        return f"{proposal.provider_ref}-tp{proposal.tensor_parallel_size}-r{proposal.replicas}"

    @staticmethod
    def _guide_variant(provider_ref: ProviderRef) -> str | None:
        if provider_ref == "pd-disaggregation":
            return "vllm"
        if provider_ref == "tiered-prefix-cache":
            return "native/cpu/base"
        return None
