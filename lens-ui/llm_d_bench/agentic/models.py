"""Contracts owned by the Agentic Deploy orchestration boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from llm_d_bench.configuration.models import ModelSecretConfiguration
from llm_d_bench.deploy.standard_kubernetes_service import StandardKubernetesServiceRequest

from .facts import PlanningEvidence
from .planner import PlannedCandidate, PlannerDecision, PlanningFacts


def utcnow() -> datetime:
    return datetime.now(UTC)


class AgenticDeploymentStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    DEPLOYING = "deploying"
    RECOMMENDED = "recommended"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgenticExecutionPolicy(BaseModel):
    mode: Literal["approval_required", "automatic"] = "approval_required"
    max_optimization_iterations: int = Field(default=0, ge=0, le=3)
    max_benchmark_runs: int = Field(default=0, ge=0, le=4)
    max_total_duration_minutes: int = Field(default=60, ge=1, le=180)


class AgenticDeploymentCreateRequest(StandardKubernetesServiceRequest):
    """Bounded Agentic request; serving parameters remain the Standard contract."""

    execution_policy: AgenticExecutionPolicy = Field(default_factory=AgenticExecutionPolicy)
    planning_facts: PlanningFacts
    planner_prompt: str | None = Field(default=None, max_length=2_000)
    ai_provider_id: str | None = Field(default=None, max_length=200)


class AgenticCandidateRefinementRequest(BaseModel):
    planner_prompt: str = Field(min_length=1, max_length=2_000)


class AgenticCandidate(BaseModel):
    id: str = "candidate-1"
    backend: str = "vllm"
    provider_ref: str = "baseline-vllm"
    replicas: int
    tensor_parallel_size: int
    prefill_replicas: int | None = None
    prefill_tensor_parallel_size: int | None = None
    guide_variant: str | None = None
    max_model_len: int
    gpu_memory_utilization: float
    evidence: list[str] = Field(default_factory=list)
    allocatable_kv_cache_gib: float | None = None
    per_request_kv_cache_gib: float | None = None
    max_concurrent_requests: int | None = None


class AgenticCandidateSelectionRequest(BaseModel):
    candidate_id: str = Field(min_length=1)


class AgenticDecisionMetadata(BaseModel):
    planner: Literal["deterministic", "openai-compatible"]
    planner_model: str | None = None
    selection_method: str
    score_method: str


class AgenticDeploymentRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: AgenticDeploymentStatus
    request: AgenticDeploymentCreateRequest
    selected_candidate: AgenticCandidate
    # Approval happens after planning, so retain the credential source that was
    # validated with the cache instead of trying to rediscover or guess it.
    model_secret: ModelSecretConfiguration = Field(default_factory=ModelSecretConfiguration)
    candidates: list[PlannedCandidate] = Field(default_factory=list)
    decision: PlannerDecision | None = None
    planning_evidence: list[PlanningEvidence] = Field(default_factory=list)
    decision_evidence_ids: list[str] = Field(default_factory=list)
    decision_metadata: AgenticDecisionMetadata | None = None
    planning_cluster_snapshot: dict[str, object] = Field(default_factory=dict)
    generator: Literal["deterministic", "ai-mcp"] = "deterministic"
    generator_model: str | None = None
    generator_fallback_reason: str | None = None
    generator_fallback_detail: str | None = None
    generator_tool_trace: list[dict[str, object]] = Field(default_factory=list)
    planning_trace: list[dict[str, object]] = Field(default_factory=list)
    generator_candidate_proposals: list[dict[str, object]] = Field(default_factory=list)
    rejected_candidates: list[PlannedCandidate] = Field(default_factory=list)
    planner: str = "deterministic"
    planner_fallback_reason: str | None = None
    planning_snapshot_id: str | None = None
    planning_snapshot_path: str | None = None
    configuration_artifact_id: str | None = None
    deployment_run_id: str | None = None
    deployment_execution_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
