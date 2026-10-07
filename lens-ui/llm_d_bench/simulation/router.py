"""FastAPI routes for Prism's Simulation dashboard."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from llm_d_bench.auth.access import current_principal, require_cluster_access, visible_cluster_ids
from llm_d_bench.cluster import require_active_session
from llm_d_bench.utils.downloads import ensure_within_root, file_download_response

from .backends import list_backends, list_scenarios
from .endpoints import discover_models
from .errors import SimulationConfigurationError
from .models import (
    BackendName,
    ModelDiscoveryRequest,
    SimulationScenario,
    SimulationTask,
    SimulationTaskCreateRequest,
    SimulationTaskListPage,
    SimulationTaskRerunRequest,
    SimulationTaskStatus,
    TraceDatasetDownloadRequest,
    TraceFormat,
)
from .service import (
    create_task,
    delete_task,
    get_response_code_issues,
    get_task,
    list_tasks,
    rerun_task,
    resume_queued_tasks,
    shutdown_service,
    stop_task,
    task_root,
)
from .traces import (
    BaseTrace,
    get_trace_timeline,
    trace_registry,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/simulation", tags=["simulation"])

# Previous router-local names remain importable without duplicating their schemas.
CreateTaskRequest = SimulationTaskCreateRequest
DownloadRequest = TraceDatasetDownloadRequest


def _task_json(task: SimulationTask) -> dict[str, Any]:
    exclude = {"live_summary"} if task.live_summary is None else None
    return task.model_dump(mode="json", exclude=exclude)


def _task_page_json(page: SimulationTaskListPage) -> dict[str, Any]:
    return {
        "tasks": [_task_json(task) for task in page.tasks],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "counts": page.counts,
        "facets": page.facets,
    }


@router.get(
    "/backends",
    summary=(
        "List Simulation backends Lens knows about and whether each backend is available in the current environment."
    ),
    description=(
        "List Simulation backends Lens knows about and whether each backend is available in the current environment."
    ),
    operation_id="list_simulation_backends",
)
async def backends_catalog() -> dict[str, Any]:
    return {"backends": [backend.model_dump(mode="json") for backend in list_backends()]}


@router.post(
    "/models/discover",
    summary="Probe a model endpoint and discover which models it reports.",
    description=(
        "Probe a model endpoint and discover which models it reports. Use this before creating a simulation task "
        "when the target endpoint is not yet fully described."
    ),
    operation_id="discover_simulation_models",
)
async def endpoint_models(request: ModelDiscoveryRequest) -> dict[str, Any]:
    try:
        return await discover_models(request.endpoint_url)
    except SimulationConfigurationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get(
    "/scenarios",
    summary=("List the simulation scenario catalog Lens exposes, such as chat, API-calling, and coding."),
    description=("List the simulation scenario catalog Lens exposes, such as chat, API-calling, and coding."),
    operation_id="list_simulation_scenarios",
)
async def scenarios_catalog() -> dict[str, Any]:
    return {"scenarios": [scenario.model_dump(mode="json") for scenario in list_scenarios()]}


@router.get(
    "/trace-datasets",
    summary="List registered trace datasets and the backends that can replay each one.",
    description="List registered trace datasets and the backends that can replay each one.",
    operation_id="list_trace_datasets",
)
async def trace_datasets_catalog() -> dict[str, Any]:
    backends = list_backends()
    datasets = []
    for dataset in trace_registry.list():
        supported = [
            backend.name
            for backend in backends
            if any(scenario.name == dataset["scenario"] for scenario in backend.scenarios)
            and dataset["trace_format"] in backend.capabilities.get("trace_formats", [])
        ]
        datasets.append({**dataset, "supported_backends": supported})
    return {"data_dir": str(BaseTrace.trace_root()), "datasets": datasets}


@router.post(
    "/trace-datasets/download",
    summary=("Download a registered trace dataset into Lens\\'s local trace store."),
    description=(
        "Download a registered trace dataset into Lens\\'s local trace store. "
        "Use this before replaying a dataset that is not yet present on disk."
    ),
    operation_id="download_trace_dataset",
)
async def download_trace_dataset(request: TraceDatasetDownloadRequest) -> dict[str, Any]:
    try:
        return await BaseTrace.download(request.dataset, request.force)
    except SimulationConfigurationError as error:
        code = 429 if "maximum number of concurrent trace downloads" in str(error) else 400
        raise HTTPException(status_code=code, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get(
    "/trace-datasets/{dataset_name}/timeline",
    summary="Get a pre-aggregated timeline view for one trace dataset.",
    description=(
        "Get a pre-aggregated timeline view for one trace dataset. "
        "Use this to inspect traffic intensity and replay shape before running Simulation."
    ),
    operation_id="get_trace_dataset_timeline",
)
async def trace_dataset_timeline(
    dataset_name: str,
    bins: int = Query(default=800, ge=50, le=2000),
) -> dict[str, Any]:
    try:
        return await get_trace_timeline(dataset_name, bins)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=error.args[0]) from error
    except SimulationConfigurationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logger.exception("Unable to build trace timeline for %s", dataset_name)
        raise HTTPException(status_code=500, detail="Unable to build trace timeline") from error


@router.get(
    "/tasks",
    summary=(
        "List Simulation tasks with optional filters for status, scenario, backend, model, dataset, endpoint, and "
        "creation window."
    ),
    description=(
        "List Simulation tasks with optional filters for status, scenario, backend, model, dataset, endpoint, and "
        "creation window."
    ),
    operation_id="list_simulation_tasks",
)
async def tasks_catalog(
    request: Request = None,
    task_status: Annotated[SimulationTaskStatus | None, Query(alias="status")] = None,
    scenario: SimulationScenario | None = None,
    search: str | None = Query(default=None, max_length=200),
    backend: BackendName | None = None,
    model: str | None = Query(default=None, max_length=200),
    dataset: str | None = Query(default=None, max_length=200),
    trace_format: TraceFormat | None = None,
    endpoint: str | None = Query(default=None, max_length=500),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    if search is not None:
        search = search.strip()
        if not search:
            raise HTTPException(status_code=422, detail="search: String must contain at least 1 character")
    try:
        page = await list_tasks(
            status=task_status,
            scenario=scenario,
            search=search,
            backend=backend,
            model=model,
            dataset=dataset,
            trace_format=trace_format,
            endpoint=endpoint,
            created_after=created_after.isoformat() if created_after else None,
            created_before=created_before.isoformat() if created_before else None,
            offset=offset,
            limit=limit,
        )
        allowed = visible_cluster_ids(current_principal(request))
        if allowed is not None:
            tasks = [task for task in page.tasks if getattr(task, "endpoint_cluster_id", None) in allowed]
            return {
                "tasks": [_task_json(task) for task in tasks],
                "total": len(tasks),
                "offset": page.offset,
                "limit": page.limit,
                "counts": page.counts,
                "facets": page.facets,
            }
        return _task_page_json(page)
    except Exception as error:
        logger.exception("Unable to list simulation tasks")
        raise HTTPException(status_code=500, detail="Unable to list simulation tasks") from error


@router.post(
    "/tasks",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a Simulation task that replays a trace dataset against a target model endpoint.",
    description="Create a Simulation task that replays a trace dataset against a target model endpoint.",
    operation_id="create_simulation_task",
)
async def create_simulation_task(request: SimulationTaskCreateRequest, http_request: Request = None) -> dict[str, Any]:
    try:
        if request.cluster_session_id:
            require_active_session(request.cluster_session_id)
        task = await create_task(request, http_request=http_request)
        return {"task_id": task.id, "status": "queued", "task": _task_json(task)}
    except SimulationConfigurationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unable to create simulation task")
        raise HTTPException(status_code=500, detail="Unable to create simulation task") from error


@router.get(
    "/tasks/{task_id}",
    summary="Get one Simulation task by id, including progress, logs, and result artifacts when they exist.",
    description="Get one Simulation task by id, including progress, logs, and result artifacts when they exist.",
    operation_id="get_simulation_task",
)
async def simulation_task(task_id: str, request: Request = None) -> dict[str, Any]:
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Simulation task not found")
    require_cluster_access(current_principal(request), getattr(task, "endpoint_cluster_id", None))
    return _task_json(task)


@router.get(
    "/tasks/{task_id}/artifacts/{kind}",
    summary="Download a simulation task artifact.",
    description=(
        "Download a saved simulation artifact by task id and artifact kind as a file attachment. Use the task result "
        "to discover available artifact kinds. Returns 404 when the task, result, or artifact file is unavailable."
    ),
    operation_id="download_simulation_artifact",
)
async def download_simulation_artifact(task_id: str, kind: str):
    """Stream a task artifact as an attachment without exposing its filesystem path."""
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Simulation task not found")
    if task.result is None:
        raise HTTPException(status_code=404, detail="Task has no artifacts yet")
    match = next((artifact for artifact in task.result.artifacts if artifact.kind == kind), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        safe_path = ensure_within_root(match.path, task_root())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not safe_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file is missing")
    return file_download_response(safe_path, media_type=match.media_type)


@router.get(
    "/tasks/{task_id}/response-code-issues",
    summary="Get requests from a Simulation task that returned a specific HTTP status code.",
    description=(
        "Get requests from a Simulation task that returned a specific HTTP status code. "
        "Use this to inspect failing or anomalous responses in detail."
    ),
    operation_id="get_simulation_response_code_issues",
)
async def simulation_response_code_issues(
    task_id: str,
    status_code: str = Query(min_length=3, max_length=7),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    if status_code == "unknown":
        parsed_status_code = None
    else:
        try:
            parsed_status_code = int(status_code)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid response status code") from error
        if not 100 <= parsed_status_code <= 599:
            raise HTTPException(status_code=422, detail="Invalid response status code")
    try:
        page = await get_response_code_issues(
            task_id,
            parsed_status_code,
            offset=offset,
            limit=limit,
        )
    except SimulationConfigurationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return page.model_dump(mode="json", by_alias=True)


@router.post(
    "/tasks/{task_id}/stop",
    summary="Stop an active Simulation task.",
    description="Stop an active Simulation task. Use this when a replay should be cancelled before it finishes.",
    operation_id="stop_simulation_task",
)
async def stop_simulation_task(task_id: str) -> dict[str, str]:
    try:
        await stop_task(task_id)
        return {"task_id": task_id, "status": "cancelling"}
    except SimulationConfigurationError as error:
        code = 404 if str(error) == "Simulation task not found" else 400
        raise HTTPException(status_code=code, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unable to stop simulation task")
        raise HTTPException(status_code=500, detail="Unable to stop simulation task") from error


@router.post(
    "/tasks/{task_id}/rerun",
    status_code=status.HTTP_202_ACCEPTED,
    summary=(
        "Queue a rerun of a previous Simulation task, optionally rebinding it to a fresh endpoint or deployment "
        "execution."
    ),
    description=(
        "Queue a rerun of a previous Simulation task, optionally rebinding it to a fresh endpoint or deployment "
        "execution."
    ),
    operation_id="rerun_simulation_task",
)
async def rerun_simulation_task(
    task_id: str,
    request: SimulationTaskRerunRequest | None = None,
    http_request: Request = None,
) -> dict[str, Any]:
    try:
        task = await rerun_task(task_id, override=request, http_request=http_request)
        return {"task_id": task.id, "status": "queued", "task": _task_json(task)}
    except SimulationConfigurationError as error:
        code = 404 if str(error) == "Simulation task not found" else 400
        raise HTTPException(status_code=code, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unable to rerun simulation task %s", task_id)
        raise HTTPException(status_code=500, detail="Unable to rerun simulation task") from error


@router.delete(
    "/tasks/{task_id}",
    summary="Delete a Simulation task record.",
    description=(
        "Delete a Simulation task record. Use this for final cleanup after a task is complete, failed, or otherwise "
        "no longer needed."
    ),
    operation_id="delete_simulation_task",
)
async def delete_simulation_task(task_id: str) -> dict[str, str]:
    try:
        await delete_task(task_id)
        return {"task_id": task_id, "status": "deleted"}
    except SimulationConfigurationError as error:
        code = 404 if str(error) == "Simulation task not found" else 409
        raise HTTPException(status_code=code, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unable to delete simulation task")
        raise HTTPException(status_code=500, detail="Unable to delete simulation task") from error


router.add_event_handler("shutdown", shutdown_service)
router.add_event_handler("startup", resume_queued_tasks)
