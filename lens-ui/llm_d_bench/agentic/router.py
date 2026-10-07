"""Independent HTTP API for Agentic Deploy orchestration."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from llm_d_bench.auth.access import current_principal, owner_for_create

from .models import (
    AgenticCandidateRefinementRequest,
    AgenticCandidateSelectionRequest,
    AgenticDeploymentCreateRequest,
    AgenticDeploymentRun,
)
from .service import service
from .streaming import planning_stream_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agentic-deployments", tags=["agentic-deployments"])


@router.post(
    "",
    response_model=AgenticDeploymentRun,
    status_code=201,
    summary="Create a new Agentic Deployment plan.",
    description=(
        "Create a new Agentic Deployment plan. Provide the standard deployment inputs plus planning facts so Lens "
        "can propose, score, and stage candidate deployment topologies."
    ),
    operation_id="create_agentic_plan",
)
async def create_agentic_deployment(
    request: AgenticDeploymentCreateRequest,
    http_request: Request,
) -> AgenticDeploymentRun:
    try:
        principal = current_principal(http_request)
        return await service.create(request, principal_id=owner_for_create(principal))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/stream", summary="Create an Agentic Deployment plan with live progress events.")
async def stream_agentic_deployment(
    request: AgenticDeploymentCreateRequest,
    http_request: Request,
) -> StreamingResponse:
    principal = current_principal(http_request)
    principal_id = owner_for_create(principal)

    return planning_stream_response(
        lambda on_progress: service.create(request, on_progress=on_progress, principal_id=principal_id),
        expected_errors=(ValueError,),
        failure_message="Unable to generate an Agentic recommendation.",
        on_unexpected_error=lambda: logger.exception("Agentic recommendation generation failed"),
    )


@router.get(
    "",
    response_model=list[AgenticDeploymentRun],
    summary=(
        "List Agentic Deployment plans (recommendations) created in this Lens instance, with their current status."
    ),
    description=(
        "List Agentic Deployment plans (recommendations) created in this Lens instance, with their current status."
    ),
    operation_id="list_agentic_plans",
)
async def list_agentic_deployments() -> list[AgenticDeploymentRun]:
    return service.list()


@router.get(
    "/{run_id}",
    response_model=AgenticDeploymentRun,
    summary=(
        "Get the full detail of one Agentic Deployment plan: candidates, evidence, scoring source (deterministic vs."
    ),
    description=(
        "Get the full detail of one Agentic Deployment plan: candidates, evidence, scoring source (deterministic vs. "
    ),
    operation_id="get_agentic_plan",
)
async def get_agentic_deployment(run_id: str) -> AgenticDeploymentRun:
    run = service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="agentic deployment run not found")
    return run


@router.post(
    "/{run_id}/select",
    response_model=AgenticDeploymentRun,
    summary="Select one candidate inside an existing Agentic Deployment plan.",
    description=(
        "Select one candidate inside an existing Agentic Deployment plan. Use this when the planner produced "
        "multiple candidates and you want to pin one before approval or refinement."
    ),
    operation_id="select_agentic_plan_candidate",
)
async def select_agentic_candidate(
    run_id: str,
    request: AgenticCandidateSelectionRequest,
) -> AgenticDeploymentRun:
    try:
        return service.select_candidate(run_id, request.candidate_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/{run_id}/refine",
    response_model=AgenticDeploymentRun,
    summary="Ask Lens to refine an Agentic Deployment plan with a new planner prompt.",
    description=(
        "Ask Lens to refine an Agentic Deployment plan with a new planner prompt. Use this to request another "
        "planning pass with tighter constraints or different priorities."
    ),
    operation_id="refine_agentic_plan",
)
async def refine_agentic_deployment(
    run_id: str,
    request: AgenticCandidateRefinementRequest,
    http_request: Request,
) -> AgenticDeploymentRun:
    try:
        principal = current_principal(http_request)
        return await service.refine(run_id, request, principal_id=owner_for_create(principal))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{run_id}/refine/stream", summary="Refine an Agentic Deployment plan with live progress events.")
async def stream_refine_agentic_deployment(
    run_id: str,
    request: AgenticCandidateRefinementRequest,
    http_request: Request,
) -> StreamingResponse:
    principal = current_principal(http_request)
    principal_id = owner_for_create(principal)

    return planning_stream_response(
        lambda on_progress: service.refine(
            run_id,
            request,
            on_progress=on_progress,
            principal_id=principal_id,
        ),
        expected_errors=(KeyError, ValueError),
        failure_message="Unable to recalculate the Agentic recommendation.",
    )


@router.post(
    "/{run_id}/approve",
    response_model=AgenticDeploymentRun,
    status_code=202,
    summary="Approve an Agentic Deployment plan so Lens can begin executing it.",
    description=(
        "Approve an Agentic Deployment plan so Lens can begin executing it. "
        "Use this only after reviewing the selected candidate and evidence."
    ),
    operation_id="approve_agentic_plan",
)
async def approve_agentic_deployment(run_id: str, request: Request = None) -> AgenticDeploymentRun:
    try:
        return await service.approve(run_id, owner_user_id=owner_for_create(current_principal(request)))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
