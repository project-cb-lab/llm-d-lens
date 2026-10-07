"""Evaluation HTTP DTOs, preserving legacy extension fields and envelopes.

Optional fields are omitted unless present in the record. Result evidence is
extensible because different benchmark harnesses produce different payloads.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .workflow_state import BenchmarkStatus, WorkflowStatus


class EvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class BenchmarkDefaultsResponse(BaseModel):
    mode: Literal["local-runtime", "managed-source"]
    repository: str
    revision: str
    localRuntimeConfigured: bool  # noqa: N815 - preserves the established JSON wire key


class BenchmarkRunResponse(EvaluationResponse):
    id: str
    status: BenchmarkStatus
    kind: Literal["benchmark"] = "benchmark"
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    cluster_id: str | None = None
    cluster_session_id: str | None = None
    deployment_execution_id: str | None = None
    deployment_ownership: str | None = None
    evaluation_workflow_id: str | None = None
    evaluation_case_id: str | None = None
    model: str | None = None
    namespace: str | None = None
    benchmark: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    matrix_results: list[dict[str, Any]] | None = None
    rate_stage_results: list[dict[str, Any]] | None = None
    deployment_cases: list[dict[str, Any]] | None = None
    resource_snapshot: dict[str, Any] | None = None
    error: str | None = None


class EvaluationCaseResponse(EvaluationResponse):
    id: str
    status: str = Field(description="Evaluation case phase, including cancelling during cleanup.")
    kind: str | None = None
    deployment_run_id: str | None = None
    deployment_case_id: str | None = None
    execution_id: str | None = None
    evaluation_run_id: str | None = None
    configuration: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    error: str | None = None
    cleanup_error: str | None = None


class EvaluationWorkflowResponse(EvaluationResponse):
    id: str
    status: WorkflowStatus
    kind: Literal["workflow"] = "workflow"
    name: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    cluster_id: str | None = None
    cluster_session_id: str | None = None
    deployment_ownership: str | None = None
    deployment_run_id: str | None = None
    evaluation_run_id: str | None = None
    cases: list[EvaluationCaseResponse] | None = None
    report: dict[str, Any] | None = None
    error: str | None = None


class BenchmarkRunListResponse(BaseModel):
    items: list[BenchmarkRunResponse]


class EvaluationWorkflowListResponse(BaseModel):
    items: list[EvaluationWorkflowResponse]


class EvaluationCaseDetailsResponse(EvaluationResponse):
    case: EvaluationCaseResponse
    deployment: Any = None
    deployment_cases: list[dict[str, Any]] = Field(default_factory=list)
    deployment_warning: str | None = None
    evaluation: BenchmarkRunResponse | None = None


class EvaluationDetailsResponse(EvaluationResponse):
    workflow: EvaluationWorkflowResponse
    cases: list[EvaluationCaseDetailsResponse] | None = None
    report: dict[str, Any] | None = None
    deployment: dict[str, Any] | None = None
    evaluation: BenchmarkRunResponse | None = None


class EvaluationCancelResponse(BaseModel):
    id: str
    status: Literal["cancelled"]


class EvaluationProblem(BaseModel):
    """Wire shape emitted by the existing application Problem Details handlers."""

    type: str
    status: int
    title: str
    detail: Any
    code: str
    requestId: str | None = None  # noqa: N815 - preserves the established problem-details wire key


def evaluation_problem_responses(*codes: int) -> dict:
    return {
        code: {
            "description": {
                401: "Authentication required",
                403: "Permission denied",
                404: "Resource not found",
                409: "Invalid lifecycle state or deployment conflict",
                422: "Invalid request",
                503: "Evaluation could not be queued",
            }[code],
            "content": {"application/problem+json": {"schema": EvaluationProblem.model_json_schema()}},
        }
        for code in codes
    }
