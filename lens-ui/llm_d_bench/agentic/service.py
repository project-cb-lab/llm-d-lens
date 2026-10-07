"""Agentic Deploy orchestration using existing Configuration and Deploy services."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from llm_d_bench.ai_providers.client import AIProviderClientError
from llm_d_bench.aic.models import AICEstimateRequest
from llm_d_bench.aic.service import AICError, estimate
from llm_d_bench.capacity import ValidationParams, evaluate_capacity, validate_vllm_params
from llm_d_bench.cluster.sessions import require_active_session
from llm_d_bench.configuration.models import ModelSecretConfiguration
from llm_d_bench.deploy.application import deployment_run_manager
from llm_d_bench.deploy.contracts import DeploymentRunCreateRequest

from .configuration import build_agentic_configuration
from .facts import resolve_planning_facts
from .generator import AICandidateGenerator, CandidateGenerationError, CandidateProposal, CandidateValidator
from .historical_candidate_seeds import HistoricalCandidateSeedRetriever
from .models import (
    AgenticCandidate,
    AgenticCandidateRefinementRequest,
    AgenticDecisionMetadata,
    AgenticDeploymentCreateRequest,
    AgenticDeploymentRun,
    AgenticDeploymentStatus,
)
from .performance_evidence import BenchmarkPerformanceEvidence
from .planner import AICCandidatePrediction, DeterministicPlanner, OpenAICompatiblePlanner, OpenAIPlannerError
from .planning_snapshot import PlanningSnapshotStore

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class AgenticDeploymentService:
    """Retain Agentic orchestration state while delegating infrastructure work."""

    def __init__(
        self,
        historical_seed_mode: str | None = None,
        snapshot_store: PlanningSnapshotStore | None = None,
    ) -> None:
        self._runs: dict[str, AgenticDeploymentRun] = {}
        enabled = os.getenv("AGENTIC_BENCHMARK_SEEDS_ENABLED", "false").lower() in {"1", "true", "yes"}
        configured_mode = historical_seed_mode or (
            os.getenv("AGENTIC_HISTORICAL_SEEDS_MODE", "shadow") if enabled else "disabled"
        )
        self._historical_seed_mode = (
            configured_mode if configured_mode in {"disabled", "shadow", "apply"} else "disabled"
        )
        self._historical_seed_retriever = HistoricalCandidateSeedRetriever()
        self._snapshot_store = snapshot_store or PlanningSnapshotStore()

    async def create(
        self,
        request: AgenticDeploymentCreateRequest,
        on_progress: ProgressCallback | None = None,
        *,
        principal_id: str | None = None,
    ) -> AgenticDeploymentRun:
        await self._emit_progress(
            on_progress,
            {
                "phase": "validation",
                "status": "running",
                "message": "Validating deployment inputs and model cache.",
            },
        )
        session = require_active_session(request.cluster_session_id)
        if request.execution_policy.mode == "automatic":
            raise ValueError("automatic Agentic execution is not enabled")
        # Validation also returns the cache entry's credential policy.  Keep it
        # with the plan because approval runs later and must not lose the
        # Secret source that made this model-cache selection valid.
        model_secret = await self._validate_model_cache(request, session.server_id)
        await self._emit_progress(
            on_progress,
            {
                "phase": "validation",
                "status": "complete",
                "message": "Deployment inputs and model cache are valid.",
            },
        )
        planning_facts = self._planning_only_facts(
            request.planning_facts,
            operator_preference=request.planner_prompt or "",
        )
        deterministic = DeterministicPlanner()
        provider = OpenAICompatiblePlanner.from_provider_id(request.ai_provider_id)
        candidates, generation, resolved = await self._generate_candidates(
            provider,
            session.server_id,
            request.model,
            planning_facts,
            request.planner_prompt,
            provider_requested=bool(request.ai_provider_id),
            on_progress=on_progress,
            principal_id=principal_id,
        )
        valid_candidate_count = sum(candidate.deployable for candidate in candidates)
        rejected_candidates = [candidate for candidate in candidates if not candidate.deployable]
        rejection_reasons = sorted(
            {reason for candidate in rejected_candidates for reason in candidate.rejection_reasons}
        )
        validation_message = (
            f"Checked {len(candidates)} candidate(s): {valid_candidate_count} valid, "
            f"{len(rejected_candidates)} rejected."
        )
        if rejection_reasons:
            validation_message += " Rejection reasons: " + "; ".join(rejection_reasons[:3]) + "."
        valid_candidate_ids = [candidate.id for candidate in candidates if candidate.deployable]
        if valid_candidate_ids:
            validation_message += " Valid candidates: " + ", ".join(valid_candidate_ids[:5]) + "."
        await self._emit_progress(
            on_progress,
            {
                "phase": "candidate_validation",
                "status": "complete",
                "message": validation_message,
            },
        )
        performance_evidence = BenchmarkPerformanceEvidence()
        historical_samples = performance_evidence.historical_samples(
            request.model,
            resolved.facts,
            candidates,
        )
        ai_candidates = [
            candidate.model_copy(
                update={
                    "historical_benchmarks": [asdict(sample) for sample in historical_samples.get(candidate.id, ())],
                }
            )
            for candidate in candidates
        ]
        deterministic_facts = replace(
            resolved.facts,
            performance_score_inputs=performance_evidence.score_inputs(
                request.model,
                resolved.facts,
                candidates,
            ),
        )
        deterministic_candidates = [
            deterministic._attach_performance_evidence(deterministic_facts, candidate) for candidate in candidates
        ]
        decision = deterministic.decide(deterministic_facts, deterministic_candidates)
        decision_facts = deterministic_facts
        selected_candidates = deterministic_candidates
        planner_name = "deterministic"
        planner_fallback_reason: str | None = None
        planner_model: str | None = None
        scored_candidates = deterministic.score(deterministic_facts, deterministic_candidates)[:3]
        scored_candidate_count = len(scored_candidates)
        deterministic_ranking = deterministic.ranking_trace(deterministic_facts, deterministic_candidates)
        if (
            provider is not None
            and decision.action == "select_candidate"
            and any(candidate.deployable for candidate in ai_candidates)
        ):
            try:
                decision, provider_candidates = await provider.recommend(
                    resolved.facts,
                    [candidate for candidate in ai_candidates if candidate.deployable],
                    operator_prompt=request.planner_prompt,
                    on_progress=on_progress,
                )
                validated_candidate_ids = {candidate.id for candidate in ai_candidates if candidate.deployable}
                if decision.candidate_id not in validated_candidate_ids:
                    raise OpenAIPlannerError("external planner selected an unvalidated candidate")
                scored_candidate_count = len(provider_candidates)
                scored_candidates = provider_candidates[:3]
                decision_facts = resolved.facts
                selected_candidates = ai_candidates
                planner_name = "openai-compatible"
                planner_model = getattr(getattr(provider, "settings", None), "model", None)
                await self._emit_progress(
                    on_progress,
                    {
                        "phase": "scoring",
                        "status": "complete",
                        "message": (
                            f"AI planner ranked {len(provider_candidates)} candidate(s) and selected "
                            f"{decision.candidate_id}."
                        ),
                    },
                )
            except OpenAIPlannerError as error:
                # Planning remains available when an optional provider is unavailable or invalid.
                logger.warning("External candidate ranking fell back: %s", error)
                decision = deterministic.decide(deterministic_facts, deterministic_candidates)
                scored_candidates = deterministic.score(deterministic_facts, deterministic_candidates)[:3]
                planner_fallback_reason = "invalid_response"
                await self._emit_progress(
                    on_progress,
                    {
                        "phase": "scoring",
                        "status": "fallback",
                        "message": "AI ranking was unavailable; deterministic ranking was used.",
                    },
                )
            except Exception as error:
                logger.warning("External candidate ranking fell back: %s", error)
                decision = deterministic.decide(deterministic_facts, deterministic_candidates)
                scored_candidates = deterministic.score(deterministic_facts, deterministic_candidates)[:3]
                planner_fallback_reason = self._planner_failure_reason(error)
                await self._emit_progress(
                    on_progress,
                    {
                        "phase": "scoring",
                        "status": "fallback",
                        "message": "AI ranking was unavailable; deterministic ranking was used.",
                    },
                )
        else:
            await self._emit_progress(
                on_progress,
                {
                    "phase": "scoring",
                    "status": "complete",
                    "message": (
                        f"Deterministic ranking evaluated {valid_candidate_count} valid candidate(s) and selected "
                        f"{decision.candidate_id}."
                    ),
                },
            )
        if decision.action != "select_candidate" or decision.candidate_id is None:
            raise ValueError("no deployable Agentic candidate: " + "; ".join(decision.unmet_constraints))
        selected = next(candidate for candidate in selected_candidates if candidate.id == decision.candidate_id)
        candidate = AgenticCandidate(
            id=selected.id,
            provider_ref=selected.provider_ref,
            replicas=selected.replicas,
            tensor_parallel_size=selected.tensor_parallel_size,
            prefill_replicas=selected.prefill_replicas,
            prefill_tensor_parallel_size=selected.prefill_tensor_parallel_size,
            guide_variant=selected.guide_variant,
            max_model_len=selected.max_model_len,
            gpu_memory_utilization=selected.gpu_memory_utilization,
            evidence=selected.evidence_ids,
        )
        run = AgenticDeploymentRun(
            status=AgenticDeploymentStatus.AWAITING_APPROVAL,
            request=request,
            selected_candidate=candidate,
            model_secret=model_secret or ModelSecretConfiguration(),
            candidates=scored_candidates,
            decision=decision,
            planning_evidence=resolved.evidence,
            decision_evidence_ids=self._decision_evidence_ids(decision_facts, selected),
            decision_metadata=self._decision_metadata(planner_name, planner_model, scored_candidate_count),
            planning_cluster_snapshot=resolved.cluster_snapshot,
            generator=generation["name"],
            generator_model=generation["model"],
            generator_fallback_reason=generation["fallback_reason"],
            generator_fallback_detail=generation["fallback_detail"],
            generator_tool_trace=generation["tool_trace"],
            planning_trace=self._planning_trace(
                request,
                planning_facts,
                resolved,
                generation,
                candidates,
                deterministic_ranking,
                scored_candidates,
                decision,
                planner_name,
                planner_fallback_reason,
            ),
            generator_candidate_proposals=generation["candidate_proposals"],
            rejected_candidates=generation["rejected"],
            planner=planner_name,
            planner_fallback_reason=planner_fallback_reason,
        )
        snapshot = self._snapshot_store.save(run, decision_facts, event="created")
        run.planning_snapshot_id = snapshot.snapshot_id
        run.planning_snapshot_path = snapshot.relative_path
        self._runs[run.id] = run
        logger.info(
            "Agentic plan %s: generator=%s planner=%s candidates=%d selected=%s fallback=%s",
            run.id,
            run.generator,
            run.planner,
            len(candidates),
            decision.candidate_id,
            run.planner_fallback_reason or run.generator_fallback_reason,
        )
        return run

    @staticmethod
    def _planning_trace(
        request,
        planning_facts,
        resolved,
        generation,
        candidates,
        deterministic_ranking,
        scored_candidates,
        decision,
        planner_name,
        planner_fallback_reason,
    ) -> list[dict[str, Any]]:
        profile = request.planning_facts.workload_profile
        return [
            {
                "phase": "input",
                "status": "complete",
                "model": request.model,
                "requested_workload": asdict(profile) if profile else None,
                "requested_ttft_slo_ms": request.planning_facts.ttft_slo_ms,
                "requested_tpot_slo_ms": request.planning_facts.tpot_slo_ms,
                "effective_context_length": planning_facts.context_length,
            },
            *resolved.trace,
            {
                "phase": "generation",
                "status": "complete",
                "source": generation["name"],
                "fallback_reason": generation["fallback_reason"],
                "fallback_detail": generation["fallback_detail"],
                "proposed_count": len(generation["candidate_proposals"]),
                "tool_calls": [{"tool": item["tool"], "status": item["status"]} for item in generation["tool_trace"]],
            },
            {
                "phase": "candidate_validation",
                "status": "complete",
                "candidates": [
                    {
                        "id": item.id,
                        "deployable": item.deployable,
                        "required_gpus": item.required_gpus,
                        "rejection_reasons": item.rejection_reasons,
                    }
                    for item in [*candidates, *generation["rejected"]]
                ],
            },
            {"phase": "deterministic_ranking", "status": "complete", "ranking": deterministic_ranking},
            {
                "phase": "scoring",
                "status": "complete",
                "source": planner_name,
                "fallback_reason": planner_fallback_reason,
                "candidates": [
                    {
                        "id": item.id,
                        "score": item.score,
                        "score_source": item.score_source,
                        "slo_status": item.slo_status,
                    }
                    for item in scored_candidates
                ],
            },
            {
                "phase": "selection",
                "status": "complete",
                "candidate_id": decision.candidate_id,
                "action": decision.action,
                "fallback_candidate_ids": decision.fallback_candidate_ids,
                "unmet_constraints": decision.unmet_constraints,
            },
        ]

    @staticmethod
    async def _emit_progress(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
        logger.info("Agentic planning phase=%s status=%s %s", event["phase"], event["status"], event.get("message", ""))
        if callback is not None:
            await callback(event)

    @staticmethod
    async def _validate_model_cache(
        request: AgenticDeploymentCreateRequest,
        cluster_id: str,
    ) -> ModelSecretConfiguration | None:
        """Validate the selected cache and return its deployment credential policy.

        Model files and model credentials have separate lifecycles: a ready
        cache does not make llm-d's required ``llm-d-hf-token`` reference
        optional.  Returning the policy here lets Agentic preserve the exact
        source instead of treating validation as a boolean gate.
        """
        if request.storage_type != "model-cache" or not request.storage_volume_id:
            raise ValueError("Agentic deployment requires a model cache storage volume")
        from llm_d_bench.deploy.router import _ready_model_cache_entry_exists
        from llm_d_bench.model_cache.service import default_service as model_cache_service
        from llm_d_bench.storage.contracts import StorageVolumeKind, StorageVolumePurpose
        from llm_d_bench.storage.service import get_ready_volume

        volume = await get_ready_volume(request.storage_volume_id, cluster_id=cluster_id)
        if StorageVolumePurpose.MODEL_CACHE not in volume.purposes:
            raise ValueError("selected storage volume is not designated for model cache use")
        if volume.kind == StorageVolumeKind.DYNAMIC_PVC:
            raise ValueError("model-cache storage for deployments cannot use dynamic-pvc volumes")
        entries = await model_cache_service().list(
            cluster_id=cluster_id,
            storage_volume_id=request.storage_volume_id,
        )
        if not _ready_model_cache_entry_exists(entries, request.model):
            raise ValueError("selected model is not ready in the selected model cache storage")
        # Select the exact entry already accepted by the readiness check and
        # preserve its token source.  The old path validated the files but
        # discarded these credentials before building the deployment.
        expected_model = request.model.strip().lower()
        entry = next(
            entry
            for entry in entries
            if str(getattr(getattr(entry.source, "huggingface", None), "repo_id", "")).strip().lower() == expected_model
            and entry.status.value == "ready"
        )
        token_source = entry.token_source
        # Translate Model Cache's token contract into Configuration's contract
        # at this boundary so downstream deployment code remains provider
        # agnostic and uses the same policy as manually authored deployments.
        # Only ``existing-secret`` is a concrete, verifiable source.  Model
        # Cache's ``host`` mode is best-effort (a missing token file is a
        # no-op), whereas Deploy's ``host`` mode is either mandatory or skipped
        # for mounted model caches; mapping it would also disable the cluster
        # default Secret fallback.  Leave it unset so that fallback applies.
        if token_source.mode.value == "existing-secret":
            return ModelSecretConfiguration(
                mode="existing-secret",
                sourceNamespace=token_source.namespace,
                sourceName=token_source.name,
            )
        return None

    @staticmethod
    def _decision_evidence_ids(facts, candidate) -> list[str]:
        evidence_ids = [
            evidence_id for evidence_id in facts.evidence_ids if evidence_id.startswith("cluster-overview:")
        ]
        if DeterministicPlanner._aic_prediction(facts, candidate) is not None:
            evidence_ids.append("aic:search")
        evidence_ids.extend(candidate.evidence_ids)
        evidence_ids.extend(f"benchmark:{sample['benchmark_id']}" for sample in candidate.historical_benchmarks)
        return list(dict.fromkeys(evidence_ids))

    @staticmethod
    def _decision_metadata(
        planner_name: str,
        planner_model: str | None,
        candidate_count: int,
    ) -> AgenticDecisionMetadata:
        if planner_name == "openai-compatible":
            return AgenticDecisionMetadata(
                planner="openai-compatible",
                planner_model=planner_model,
                selection_method=(
                    f"The configured model selected and scored {candidate_count} Prism-validated candidate(s); "
                    "the highest score is selected and deterministic rank breaks ties."
                ),
                score_method=(
                    "Relative model rank normalized so the selected candidate is 100%; "
                    "it is not confidence or a benchmark measurement."
                ),
            )
        return AgenticDecisionMetadata(
            planner="deterministic",
            selection_method=(
                "Prism applies live resource constraints and operator preference first, uses exact AIC predictions "
                "only within supported aggregated or disaggregated topologies, and treats missing AIC data for other "
                "guides as neutral."
            ),
            score_method=(
                "Relative rank among validated candidates, normalized to 0% to 100%; it is not a benchmark measurement."
            ),
        )

    @staticmethod
    def _planner_failure_reason(error: Exception) -> str:
        if isinstance(error, (TypeError, ValueError, OpenAIPlannerError)):
            return "invalid_response"
        if isinstance(error, TimeoutError):
            return "timeout"
        if isinstance(error, AIProviderClientError):
            return "transport"
        return "unavailable"

    @staticmethod
    def _generator_failure_reason(error: CandidateGenerationError) -> str:
        message = str(error).lower()
        if "mcp tool" in message:
            return "mcp_unavailable"
        if any(
            fragment in message
            for fragment in (
                "invalid response",
                "no tool calls",
                "tool-round limit",
                "too many tool calls",
                "wrong cluster",
                "tool-result budget",
                "mixed submission",
                "must first call only",
            )
        ):
            return "invalid_response"
        return "provider_unavailable"

    async def _generate_candidates(
        self,
        provider: Any,
        cluster_id: str,
        model: str,
        facts,
        operator_prompt: str | None,
        *,
        provider_requested: bool,
        on_progress: ProgressCallback | None = None,
        principal_id: str | None = None,
    ) -> tuple[list, dict[str, Any], Any]:
        generation: dict[str, Any] = {
            "name": "deterministic",
            "model": None,
            "fallback_reason": "provider_unavailable" if provider_requested and provider is None else None,
            "fallback_detail": None,
            "tool_trace": [],
            "candidate_proposals": [],
            "rejected": [],
        }
        if provider is None or not hasattr(provider, "settings"):
            await self._emit_progress(
                on_progress,
                {
                    "phase": "generation",
                    "status": "running",
                    "message": "Generating candidates with deterministic planning.",
                },
            )
            resolved = await resolve_planning_facts(cluster_id, model, facts)
            candidates = DeterministicPlanner().candidates(resolved.facts)
            if provider_requested:
                candidates, resolved.facts = await self._estimate_fallback_topologies(
                    model, candidates, resolved.facts, generation["tool_trace"],
                )
            candidates, seed_count = await self._merge_historical_seeds(
                model,
                resolved.facts,
                candidates,
                on_progress=on_progress,
            )
            generation["historical_seed_count"] = seed_count
            await self._emit_progress(
                on_progress,
                {
                    "phase": "generation",
                    "status": "complete",
                    "message": f"Deterministic planning generated {len(candidates)} candidate(s).",
                },
            )
            return candidates, generation, resolved
        try:
            await self._emit_progress(
                on_progress,
                {
                    "phase": "generation",
                    "status": "running",
                    "message": "AI generator is gathering planning evidence.",
                },
            )
            result = await AICandidateGenerator(provider.settings, principal_id=principal_id).generate(
                cluster_id,
                model,
                facts,
                operator_prompt,
                on_progress=on_progress,
            )
            generation["tool_trace"] = result.proposals.tool_trace
            generation["candidate_proposals"] = [proposal.model_dump() for proposal in result.proposals.candidates]
            validated = CandidateValidator().validate(result.resolved.facts, result.proposals.candidates)
            generation["rejected"] = validated.rejected
            if validated.accepted:
                validated.accepted, result.resolved.facts = await self._estimate_missing_topologies(
                    model,
                    validated.accepted,
                    result.resolved.facts,
                    generation["tool_trace"],
                )
            if not validated.accepted:
                generation["fallback_reason"] = "all_ai_candidates_invalid"
                await self._emit_progress(
                    on_progress,
                    {
                        "phase": "generation",
                        "status": "fallback",
                        "message": (
                            "All AI-generated candidates failed validation; deterministic generation is continuing."
                        ),
                    },
                )
                candidates = DeterministicPlanner().candidates(result.resolved.facts)
                candidates, result.resolved.facts = await self._estimate_fallback_topologies(
                    model, candidates, result.resolved.facts, generation["tool_trace"],
                )
                candidates, seed_count = await self._merge_historical_seeds(
                    model,
                    result.resolved.facts,
                    candidates,
                    on_progress=on_progress,
                )
                generation["historical_seed_count"] = seed_count
                return candidates, generation, result.resolved
            candidates, seed_count = await self._merge_historical_seeds(
                model,
                result.resolved.facts,
                validated.accepted,
                on_progress=on_progress,
            )
            generation["historical_seed_count"] = seed_count
            generation.update({"name": "ai-mcp", "model": provider.settings.model, "fallback_reason": None})
            return candidates, generation, result.resolved
        except CandidateGenerationError as error:
            logger.warning("AI candidate generation fell back: %s", error)
            generation["fallback_reason"] = self._generator_failure_reason(error)
            if any(
                limit in str(error).lower()
                for limit in ("too many tool calls", "tool-round limit", "tool-result budget")
            ):
                generation["fallback_detail"] = str(error)
            await self._emit_progress(
                on_progress,
                {
                    "phase": "generation",
                    "status": "fallback",
                    "message": "AI candidate generation was unavailable; deterministic generation is continuing.",
                },
            )
            resolved = await resolve_planning_facts(cluster_id, model, facts)
            candidates = DeterministicPlanner().candidates(resolved.facts)
            candidates, resolved.facts = await self._estimate_fallback_topologies(
                model, candidates, resolved.facts, generation["tool_trace"],
            )
            candidates, seed_count = await self._merge_historical_seeds(
                model,
                resolved.facts,
                candidates,
                on_progress=on_progress,
            )
            generation["historical_seed_count"] = seed_count
            return candidates, generation, resolved

    @classmethod
    async def _estimate_fallback_topologies(cls, model, candidates, facts, tool_trace):
        priority_candidates = DeterministicPlanner().score(facts, candidates)[:3]
        estimated, facts = await cls._estimate_missing_topologies(model, priority_candidates, facts, tool_trace)
        estimates_by_id = {candidate.id: candidate for candidate in estimated}
        return [estimates_by_id.get(candidate.id, candidate) for candidate in candidates], facts

    async def _merge_historical_seeds(
        self, model: str, facts, candidates: list, *, on_progress=None
    ) -> tuple[list, int]:
        if self._historical_seed_mode == "disabled":
            return candidates, 0
        seeds = self._historical_seed_retriever.retrieve(model, facts)
        validated_seeds: list = []
        for seed in seeds:
            validation = CandidateValidator().validate(facts, [seed.proposal])
            for candidate in validation.accepted:
                validated_seeds.append(
                    candidate.model_copy(
                        update={
                            "evidence_ids": list(dict.fromkeys([*candidate.evidence_ids, *seed.evidence_refs])),
                        }
                    )
                )
        await self._emit_progress(
            on_progress,
            {
                "phase": "generation",
                "status": "complete" if self._historical_seed_mode == "apply" else "shadow",
                "message": (
                    f"Qualified {len(validated_seeds)} historical candidate seed(s)."
                    if self._historical_seed_mode == "apply"
                    else (
                        f"Observed {len(validated_seeds)} qualified historical candidate seed(s) without applying them."
                    )
                ),
            },
        )
        if self._historical_seed_mode == "shadow":
            return candidates, len(validated_seeds)
        merged = {candidate.id: candidate for candidate in candidates}
        for seed_candidate in validated_seeds:
            existing = merged.get(seed_candidate.id)
            if existing is None:
                merged[seed_candidate.id] = seed_candidate
                continue
            merged[seed_candidate.id] = existing.model_copy(
                update={
                    "evidence_ids": list(dict.fromkeys([*existing.evidence_ids, *seed_candidate.evidence_ids])),
                }
            )
        return list(merged.values()), len(validated_seeds)

    @staticmethod
    async def _estimate_missing_topologies(model, candidates, facts, tool_trace):
        searches = [item.get("arguments") for item in tool_trace if item.get("tool") == "search_candidates"]
        arguments = (searches[-1] or {}) if searches else {}
        profile = facts.workload_profile
        workload = arguments.get("workload") or {
            "isl": profile.mean_input_tokens if profile else facts.context_length,
            "osl": profile.mean_output_tokens if profile else 256,
        }
        config = arguments.get("searchConfig") or {}
        predictions = list(facts.aic_predictions)
        updated = []
        for candidate in candidates:
            if candidate.provider_ref not in {"baseline-vllm", "optimized-baseline", "pd-disaggregation"}:
                updated.append(candidate)
                continue
            if DeterministicPlanner._aic_prediction(facts, candidate):
                updated.append(candidate)
                continue
            is_pd = candidate.provider_ref == "pd-disaggregation"
            try:
                request = AICEstimateRequest(
                    model_name=model,
                    scenario="pd_disaggregation" if is_pd else "inference_scheduling",
                    gpu_count=candidate.required_gpus,
                    mean_input_tokens=int(workload.get("isl") or facts.context_length),
                    mean_output_tokens=int(workload.get("osl") or 256),
                    aic_system_name=config.get("aicSystemName") or "b60",
                    aic_backend_name=config.get("aicBackendName") or "vllm",
                    aic_database_mode=config.get("aicDatabaseMode") or "SILICON",
                    tp=candidate.tensor_parallel_size,
                    replicas=candidate.replicas,
                    prefill_tp=candidate.prefill_tensor_parallel_size or 1,
                    prefill_replicas=candidate.prefill_replicas or 1,
                    decode_tp=candidate.tensor_parallel_size,
                    decode_replicas=candidate.replicas,
                )
                result = await asyncio.wait_for(estimate(request), timeout=20)
                prediction = AICCandidatePrediction(
                    mode="disagg" if is_pd else "agg",
                    tensor_parallel_size=candidate.tensor_parallel_size,
                    replicas=candidate.replicas,
                    prefill_tensor_parallel_size=candidate.prefill_tensor_parallel_size if is_pd else None,
                    prefill_replicas=candidate.prefill_replicas if is_pd else None,
                    ttft_ms=result.get("ttft_ms"),
                    tpot_ms=result.get("tpot_ms"),
                    throughput_tokens_per_sec=result.get("throughput_tokens_per_sec"),
                )
                predictions.append(prediction)
                updated.append(
                    candidate.model_copy(
                        update={
                            "performance_estimate": {
                                "ttft_ms": prediction.ttft_ms,
                                "tpot_ms": prediction.tpot_ms,
                                "throughput_tokens_per_sec": prediction.throughput_tokens_per_sec,
                            },
                            "performance_estimate_source": "aic_estimate",
                        }
                    )
                )
            except (AICError, ValueError, TimeoutError) as error:
                logger.info("AIC estimate unavailable for candidate %s: %s", candidate.id, error)
                updated.append(candidate)
        return updated, replace(facts, aic_predictions=tuple(predictions))

    async def refine(
        self,
        run_id: str,
        request: AgenticCandidateRefinementRequest,
        on_progress: ProgressCallback | None = None,
        *,
        principal_id: str | None = None,
    ) -> AgenticDeploymentRun:
        await self._emit_progress(
            on_progress,
            {
                "phase": "validation",
                "status": "running",
                "message": "Validating the current plan and refinement request.",
            },
        )
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError("agentic deployment run not found")
        if run.status != AgenticDeploymentStatus.AWAITING_APPROVAL:
            raise ValueError("only an awaiting approval Agentic deployment can be recalculated")
        provider = OpenAICompatiblePlanner.from_provider_id(run.request.ai_provider_id)
        if provider is None:
            raise ValueError("an OpenAI-compatible planner is not configured")

        session = require_active_session(run.request.cluster_session_id)
        await self._emit_progress(
            on_progress,
            {
                "phase": "validation",
                "status": "complete",
                "message": "The current plan is eligible for recalculation.",
            },
        )
        planning_facts = self._planning_only_facts(
            run.request.planning_facts,
            operator_preference=request.planner_prompt,
        )
        deterministic = DeterministicPlanner()
        candidates, generation, refreshed = await self._generate_candidates(
            provider,
            session.server_id,
            run.request.model,
            planning_facts,
            request.planner_prompt,
            provider_requested=True,
            on_progress=on_progress,
            principal_id=principal_id,
        )
        valid_candidate_count = sum(candidate.deployable for candidate in candidates)
        rejected_candidates = [candidate for candidate in candidates if not candidate.deployable]
        rejection_reasons = sorted(
            {reason for candidate in rejected_candidates for reason in candidate.rejection_reasons}
        )
        validation_message = (
            f"Checked {len(candidates)} candidate(s): {valid_candidate_count} valid, "
            f"{len(rejected_candidates)} rejected."
        )
        if rejection_reasons:
            validation_message += " Rejection reasons: " + "; ".join(rejection_reasons[:3]) + "."
        valid_candidate_ids = [candidate.id for candidate in candidates if candidate.deployable]
        if valid_candidate_ids:
            validation_message += " Valid candidates: " + ", ".join(valid_candidate_ids[:5]) + "."
        await self._emit_progress(
            on_progress,
            {
                "phase": "candidate_validation",
                "status": "complete",
                "message": validation_message,
            },
        )
        refreshed = refreshed.model_copy(
            update={
                "facts": replace(
                    refreshed.facts,
                    performance_score_inputs=BenchmarkPerformanceEvidence().score_inputs(
                        run.request.model,
                        refreshed.facts,
                        candidates,
                    ),
                )
            }
        )
        candidates = [
            deterministic._attach_performance_evidence(refreshed.facts, candidate) for candidate in candidates
        ]
        decision = deterministic.decide(refreshed.facts, candidates)
        if decision.action != "select_candidate" or decision.candidate_id is None:
            raise ValueError("no deployable Agentic candidate: " + "; ".join(decision.unmet_constraints))

        planner_name = "openai-compatible"
        planner_fallback_reason: str | None = None
        planner_model = getattr(getattr(provider, "settings", None), "model", None)
        try:
            decision, scored_candidates = await provider.recommend(
                refreshed.facts,
                [candidate for candidate in candidates if candidate.deployable],
                operator_prompt=request.planner_prompt,
                on_progress=on_progress,
            )
            validated_candidate_ids = {candidate.id for candidate in candidates if candidate.deployable}
            if decision.candidate_id not in validated_candidate_ids:
                raise OpenAIPlannerError("external planner selected an unvalidated candidate")
            await self._emit_progress(
                on_progress,
                {
                    "phase": "scoring",
                    "status": "complete",
                    "message": f"AI planner selected {decision.candidate_id} after recalculation.",
                },
            )
        except OpenAIPlannerError as error:
            logger.warning("External candidate ranking fell back during recalculation: %s", error)
            decision = deterministic.decide(refreshed.facts, candidates)
            scored_candidates = OpenAICompatiblePlanner._diverse_candidate_catalog(refreshed.facts, candidates)[:3]
            planner_name = "deterministic"
            planner_model = None
            planner_fallback_reason = self._planner_failure_reason(error)
            await self._emit_progress(
                on_progress,
                {
                    "phase": "scoring",
                    "status": "fallback",
                    "message": f"AI reranking was unavailable; deterministic ranking selected {decision.candidate_id}.",
                },
            )
        except Exception as error:
            logger.warning("External candidate ranking fell back during recalculation: %s", error)
            decision = deterministic.decide(refreshed.facts, candidates)
            scored_candidates = OpenAICompatiblePlanner._diverse_candidate_catalog(refreshed.facts, candidates)[:3]
            planner_name = "deterministic"
            planner_model = None
            planner_fallback_reason = self._planner_failure_reason(error)
            await self._emit_progress(
                on_progress,
                {
                    "phase": "scoring",
                    "status": "fallback",
                    "message": f"AI reranking was unavailable; deterministic ranking selected {decision.candidate_id}.",
                },
            )
        selected = next(candidate for candidate in scored_candidates if candidate.id == decision.candidate_id)
        run.selected_candidate = self._agentic_candidate(selected)
        run.candidates = scored_candidates
        run.decision = decision
        run.planning_evidence = refreshed.evidence
        run.decision_evidence_ids = self._decision_evidence_ids(refreshed.facts, selected)
        run.decision_metadata = self._decision_metadata(
            planner_name,
            planner_model,
            len(scored_candidates),
        )
        run.planning_cluster_snapshot = refreshed.cluster_snapshot
        run.generator = generation["name"]
        run.generator_model = generation["model"]
        run.generator_fallback_reason = generation["fallback_reason"]
        run.generator_fallback_detail = generation["fallback_detail"]
        run.generator_tool_trace = generation["tool_trace"]
        run.planning_trace = self._planning_trace(
            run.request,
            planning_facts,
            refreshed,
            generation,
            candidates,
            deterministic.ranking_trace(refreshed.facts, candidates),
            scored_candidates,
            decision,
            planner_name,
            planner_fallback_reason,
        )
        run.generator_candidate_proposals = generation["candidate_proposals"]
        run.rejected_candidates = generation["rejected"]
        run.planner = planner_name
        run.planner_fallback_reason = planner_fallback_reason
        run.request = run.request.model_copy(update={"planner_prompt": request.planner_prompt})
        run.updated_at = datetime.now(UTC)
        snapshot = self._snapshot_store.save(run, refreshed.facts, event="refined")
        run.planning_snapshot_id = snapshot.snapshot_id
        run.planning_snapshot_path = snapshot.relative_path
        logger.info(
            "Agentic plan %s recalculated: generator=%s planner=%s candidates=%d selected=%s fallback=%s",
            run.id,
            run.generator,
            run.planner,
            len(candidates),
            decision.candidate_id,
            run.planner_fallback_reason or run.generator_fallback_reason,
        )
        return run

    @staticmethod
    def _agentic_candidate(selected):
        return AgenticCandidate(
            id=selected.id,
            provider_ref=selected.provider_ref,
            replicas=selected.replicas,
            tensor_parallel_size=selected.tensor_parallel_size,
            prefill_replicas=selected.prefill_replicas,
            prefill_tensor_parallel_size=selected.prefill_tensor_parallel_size,
            guide_variant=selected.guide_variant,
            max_model_len=selected.max_model_len,
            gpu_memory_utilization=selected.gpu_memory_utilization,
            evidence=selected.evidence_ids,
            allocatable_kv_cache_gib=getattr(selected, "allocatable_kv_cache_gib", None),
            per_request_kv_cache_gib=getattr(selected, "per_request_kv_cache_gib", None),
            max_concurrent_requests=getattr(selected, "max_concurrent_requests", None),
        )

    @staticmethod
    def _planning_only_facts(facts, *, operator_preference: str = ""):
        return replace(
            facts,
            context_length=4096,
            operator_preference=operator_preference,
            vllm_arguments=(),
        )

    def list(self) -> list[AgenticDeploymentRun]:
        return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)

    def get(self, run_id: str) -> AgenticDeploymentRun | None:
        return self._runs.get(run_id)

    def select_candidate(self, run_id: str, candidate_id: str) -> AgenticDeploymentRun:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError("agentic deployment run not found")
        if run.status != AgenticDeploymentStatus.AWAITING_APPROVAL:
            raise ValueError("only an awaiting approval Agentic deployment can change candidate")
        selected = next((candidate for candidate in run.candidates if candidate.id == candidate_id), None)
        if selected is None or not selected.deployable:
            raise ValueError("candidate is not a validated alternative for this Agentic deployment")
        run.selected_candidate = self._agentic_candidate(selected)
        if run.decision is not None:
            run.decision = run.decision.model_copy(
                update={
                    "candidate_id": selected.id,
                    "rationale": "Selected by the user from this plan's validated alternatives.",
                }
            )
        run.updated_at = datetime.now(UTC)
        return run

    async def approve(self, run_id: str, *, owner_user_id: str | None = None) -> AgenticDeploymentRun:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError("agentic deployment run not found")
        if run.status != AgenticDeploymentStatus.AWAITING_APPROVAL:
            raise ValueError("only an awaiting approval Agentic deployment can be approved")

        session = require_active_session(run.request.cluster_session_id)
        selected = next(candidate for candidate in run.candidates if candidate.id == run.selected_candidate.id)
        refreshed = await resolve_planning_facts(
            session.server_id,
            run.request.model,
            self._planning_only_facts(run.request.planning_facts),
            include_supplementary_evidence=False,
        )
        if selected.required_gpus > refreshed.facts.free_gpu_count:
            raise ValueError("selected Agentic candidate no longer has sufficient free accelerator cards")
        proposal = CandidateProposal(
            provider_ref=selected.provider_ref,
            replicas=selected.replicas,
            tensor_parallel_size=selected.tensor_parallel_size,
            prefill_replicas=selected.prefill_replicas,
            prefill_tensor_parallel_size=selected.prefill_tensor_parallel_size,
            rationale="Revalidate the selected candidate before deployment.",
            evidence_ids=selected.evidence_ids,
        )
        validation = CandidateValidator().validate(refreshed.facts, [proposal])
        refreshed_selected = (validation.accepted or validation.rejected)[0]
        if not refreshed_selected.deployable:
            raise ValueError(
                "selected Agentic candidate no longer satisfies current resource constraints: "
                + "; ".join(refreshed_selected.rejection_reasons)
            )

        # Pre-flight capacity guardrail check
        capacity_res = evaluate_capacity(ValidationParams(
            models=[run.request.model],
            gpu_memory=refreshed.facts.vram_per_gpu_gib,
            tp=selected.tensor_parallel_size,
            accelerator_nr=refreshed.facts.free_gpu_count,
            gpu_memory_util=selected.gpu_memory_utilization,
            max_model_len=selected.max_model_len,
            replicas=selected.replicas,
            model_config=refreshed.facts.model_config_dict,
            fallback_weight_gib=refreshed.facts.model_weight_gib,
        ))
        if not capacity_res.is_deployable and refreshed.facts.model_config_dict is not None:
            raise ValueError(
                "selected Agentic candidate fails pre-flight capacity validation: "
                + "; ".join(capacity_res.rejection_reasons)
            )

        # Use the credential policy captured during planning.  This keeps the
        # approve path deterministic and ensures ConfigurationAdapter can turn
        # ``modelSecret`` into the provider's deployment policy.
        configuration = build_agentic_configuration(run.request, selected, run.model_secret)
        configuration.provenance["cluster_ref"] = {
            "id": session.server_id,
            "name": session.server_id,
            "session_id": session.id,
            "connection": "managed-kubeconfig",
        }
        deployment = await deployment_run_manager.start_run(
            DeploymentRunCreateRequest(
                configurations=[configuration],
                provenance={
                    "deployment_source": {"kind": "agentic-deployment"},
                    "cluster_session_id": session.id,
                    "deployment_name": run.request.deployment_name,
                    "agentic_deployment_run_id": run.id,
                },
            ),
            owner_user_id=owner_user_id,
        )
        run.deployment_run_id = deployment.id
        run.status = AgenticDeploymentStatus.DEPLOYING
        run.updated_at = datetime.now(UTC)
        return run


service = AgenticDeploymentService()
