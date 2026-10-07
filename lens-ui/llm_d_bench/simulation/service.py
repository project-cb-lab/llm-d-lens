"""Simulation task lifecycle and persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from llm_d_bench.db.dao.simulation_task import SimulationTaskDao
from llm_d_bench.utils.artifact_store import artifact_uri, register_artifacts
from llm_d_bench.utils.paths import storage_path

from .backends import (
    completion_timeline_from_artifacts,
    get_backend,
    goodput_timeline_from_artifacts,
    latency_timelines_from_artifacts,
    list_backends,
    live_summary_from_artifacts,
    rate_timelines_from_artifacts,
    response_code_issues_from_artifacts,
    status_code_breakdown_from_artifacts,
)
from .errors import (
    SimulationCancelledError,
    SimulationConfigurationError,
    SimulationError,
    SimulationResultParseError,
)
from .models import (
    BackendName,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationResponseCodeIssuePage,
    SimulationScenario,
    SimulationTask,
    SimulationTaskCreateRequest,
    SimulationTaskListPage,
    SimulationTaskRerunRequest,
    SimulationTaskStatus,
    SimulationTrace,
    TraceFormat,
)
from .process import RunContext
from .traces import trace_registry

logger = logging.getLogger(__name__)
TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")

tasks: dict[str, SimulationTask] = {}
controllers: dict[str, asyncio.Event] = {}
runners: dict[str, asyncio.Task[None]] = {}
save_locks: dict[str, asyncio.Lock] = {}
save_jobs: dict[str, set[asyncio.Task[None]]] = {}
pending_creates = 0
create_lock: asyncio.Lock | None = None
persist_lock: asyncio.Lock | None = None
run_semaphore: asyncio.Semaphore | None = None
service_instance_id = str(uuid.uuid4())
_dao: SimulationTaskDao | None = None
_repository_thread_lock = threading.Lock()
PER_REQUEST_FILENAME = "per_request.json"
RESULT_EXTRA_FILENAME = "result_extra.json"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _task_dao() -> SimulationTaskDao:
    global _dao
    if _dao is None:
        _dao = SimulationTaskDao()
    return _dao


def _positive_environment(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def task_root() -> Path:
    return storage_path("data", "artifacts", "simulations")


def _get_create_lock() -> asyncio.Lock:
    global create_lock
    if create_lock is None:
        create_lock = asyncio.Lock()
    return create_lock


def _get_persist_lock() -> asyncio.Lock:
    global persist_lock
    if persist_lock is None:
        persist_lock = asyncio.Lock()
    return persist_lock


def _get_run_semaphore() -> asyncio.Semaphore:
    global run_semaphore
    if run_semaphore is None:
        run_semaphore = asyncio.Semaphore(_positive_environment("SIMULATION_MAX_CONCURRENT_TASKS", 1))
    return run_semaphore


def _schedule_task(task: SimulationTask) -> None:
    task_id = task.id
    if task_id in runners or task.status != "queued":
        return

    async def runner() -> None:
        await asyncio.sleep(0)
        async with _get_run_semaphore():
            if task.status == "queued":
                await _run_task(task)

    running = asyncio.create_task(runner())
    runners[task_id] = running

    def completed(finished: asyncio.Task[None]) -> None:
        if runners.get(task_id) is finished:
            runners.pop(task_id, None)

    running.add_done_callback(completed)


def _set_artifact_references(task: SimulationTask, manifest: dict | None = None) -> None:
    """Expose references only for successfully registered payloads."""
    task.artifact_uri = artifact_uri("simulation", task.id) if manifest else None
    if task.result:
        root = Path(task.task_dir).resolve()
        registered = {entry["path"]: entry["uri"] for entry in manifest["files"]} if manifest else {}
        updated = []
        for item in task.result.artifacts:
            path = Path(item.path).resolve()
            uri = registered.get(path.relative_to(root).as_posix()) if path.is_relative_to(root) else None
            updated.append(item.model_copy(update={"uri": uri}))
        task.result.artifacts = updated


async def save_task(task: SimulationTask) -> None:
    lock = save_locks.setdefault(task.id, asyncio.Lock())
    async with lock, _get_persist_lock():
        directory = Path(task.task_dir)
        directory.mkdir(parents=True, exist_ok=True)
        _set_artifact_references(task)
        task.artifact_error = None
        if task.status not in {"completed", "failed", "cancelled"}:
            await asyncio.to_thread(_persist_task, task)
            return
        await asyncio.to_thread(_persist_task, task)
        try:
            dataset = trace_registry.get_variant(task.prompt.dataset.name) if task.prompt.dataset.name else None
        except SimulationConfigurationError:
            dataset = None
        source = {
            "backend": task.simulation.backend,
            "backend_version": (
                task.result.backend_version
                if task.result and task.result.backend_version and not task.backend_version_inferred
                else task.backend_version or "unknown"
            ),
            "dataset": task.prompt.dataset.name or "unknown",
            "dataset_source": (dataset.catalog_entry().get("url") or dataset.source_repository or "unknown")
            if dataset
            else "unknown",
        }
        files = {
            path.relative_to(directory).as_posix(): {
                "truncated": task.artifacts_incomplete or task.status in {"failed", "cancelled"}
            }
            for path in directory.rglob("*")
            if path.is_file()
            and path.name != "manifest.json"
            and not path.name.endswith((".tmp", ".lock", ".manifest.json"))
        }
        try:
            manifest = await asyncio.to_thread(
                register_artifacts,
                directory,
                owner_type="simulation",
                owner_id=task.id,
                source_version=source,
                status=task.status,
                configuration_ids=task.configuration_ids,
                truncated=task.logs_truncated,
                files=files,
            )
        except Exception as error:
            task.artifact_error = f"Artifact registration failed: {error}"
            logger.exception("Unable to register simulation artifacts for %s", task.id)
            (directory / "manifest.json").unlink(missing_ok=True)
            await asyncio.to_thread(_persist_task, task)
            raise
        _set_artifact_references(task, manifest)
        await asyncio.to_thread(_persist_task, task)


def _queue_save(task: SimulationTask) -> None:
    job = asyncio.create_task(save_task(task))
    jobs = save_jobs.setdefault(task.id, set())
    jobs.add(job)

    def completed(finished: asyncio.Task[None]) -> None:
        jobs.discard(finished)
        if not jobs:
            save_jobs.pop(task.id, None)
        try:
            finished.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unable to save simulation task %s", task.id)

    job.add_done_callback(completed)


async def _await_saves(task_id: str) -> None:
    while save_jobs.get(task_id):
        await asyncio.gather(*list(save_jobs[task_id]), return_exceptions=True)


async def load_task(task_id: str) -> SimulationTask | None:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        return None
    active = tasks.get(task_id)
    if active is not None and task_id in runners:
        return active
    try:
        task = await asyncio.to_thread(_repository_get, task_id)
        if task is None:
            task = _load_legacy_task(task_id)
            if task is None:
                return None
            await save_task(task)
    except FileNotFoundError:
        return None
    except (OSError, ValidationError, ValueError):
        logger.exception("Unable to load simulation task %s: invalid task data", task_id)
        return None
    if task.id != task_id or Path(task.task_dir).resolve() != task_root() / task_id:
        logger.error("Unable to load simulation task %s: invalid task data", task_id)
        return None
    _set_artifact_references(task)
    try:
        manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text(encoding="utf-8"))
        if (manifest["owner_type"], manifest["owner_id"]) == ("simulation", task.id):
            _set_artifact_references(task, manifest)
    except (OSError, ValueError, KeyError, TypeError):
        # Legacy or interrupted registration: task remains readable without advertised artifacts.
        pass
    if task.status == "running" and task_id not in runners:
        task.status = "failed"
        task.progress_message = "Simulation interrupted by service restart"
        task.error_message = (
            "Simulation runner is no longer active"
            if task.owner_instance_id == service_instance_id
            else "Simulation belongs to a previous service instance"
        )
        task.completed_at = _now()
        await save_task(task)
    tasks[task_id] = task
    if task.status == "queued":
        _schedule_task(task)
    return task


async def get_task(task_id: str) -> SimulationTask | None:
    task = await load_task(task_id)
    if task is None:
        return None
    result = task.result
    if result is not None and not result.backend_version:
        descriptor = get_backend(result.backend or task.simulation.backend).descriptor()
        if descriptor.version:
            result.backend_version = descriptor.version
            task.backend_version_inferred = True
            await save_task(task)
    if task.status == "running":
        response = task.model_copy(deep=True)
        response.live_summary = await asyncio.to_thread(live_summary_from_artifacts, task)
        return response
    summary = task.result.summary if task.result is not None else None
    timeline = summary.get("completion_timeline") if isinstance(summary, dict) else None
    needs_timeline = not isinstance(timeline, list) or (
        bool(timeline)
        and (
            "arrived_requests" not in timeline[0]
            or "successful_requests" not in timeline[0]
            or "failed_requests" not in timeline[0]
        )
    )
    if isinstance(summary, dict) and needs_timeline:
        try:
            summary["completion_timeline"] = await asyncio.to_thread(completion_timeline_from_artifacts, task)
            await save_task(task)
        except SimulationResultParseError:
            logger.warning("Unable to derive completion timeline for task %s", task_id, exc_info=True)
    if isinstance(summary, dict) and (
        "latency_timeline" not in summary
        or "ttft_timeline" not in summary
        or "tpot_timeline" not in summary
        or "ttft_heatmap" not in summary
        or "tpot_heatmap" not in summary
        or not isinstance(summary.get("tpot_heatmap"), dict)
        or summary["tpot_heatmap"].get("sequence_length") != "output"
        or summary["tpot_heatmap"].get("sequence_length_source") != "requested"
    ):
        try:
            summary.update(await asyncio.to_thread(latency_timelines_from_artifacts, task))
            await save_task(task)
        except SimulationResultParseError:
            logger.warning("Unable to derive latency timeline for task %s", task_id, exc_info=True)
    throughput_timeline = summary.get("throughput_timeline") if isinstance(summary, dict) else None
    needs_rate_timelines = (
        (
            not isinstance(throughput_timeline, list)
            or (bool(throughput_timeline) and "request_arrival_rps" not in throughput_timeline[0])
            or "error_timeline" not in summary
        )
        if isinstance(summary, dict)
        else False
    )
    if isinstance(summary, dict) and needs_rate_timelines:
        try:
            throughput_timeline, error_timeline = await asyncio.to_thread(rate_timelines_from_artifacts, task)
            summary["throughput_timeline"] = throughput_timeline
            summary["error_timeline"] = error_timeline
            await save_task(task)
        except SimulationResultParseError:
            logger.warning("Unable to derive throughput and error timelines for task %s", task_id, exc_info=True)
    if isinstance(summary, dict) and "goodput_timeline" not in summary:
        try:
            summary["goodput_timeline"] = await asyncio.to_thread(goodput_timeline_from_artifacts, task)
            await save_task(task)
        except SimulationResultParseError:
            logger.warning("Unable to derive goodput timeline for task %s", task_id, exc_info=True)
    if isinstance(summary, dict) and "status_code_breakdown" not in summary:
        try:
            summary["status_code_breakdown"] = await asyncio.to_thread(status_code_breakdown_from_artifacts, task)
            await save_task(task)
        except SimulationResultParseError:
            logger.warning("Unable to derive response status breakdown for task %s", task_id, exc_info=True)
    return task


async def get_response_code_issues(
    task_id: str,
    status_code: int | None,
    *,
    offset: int = 0,
    limit: int = 100,
) -> SimulationResponseCodeIssuePage:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    issue_slice = await asyncio.to_thread(
        response_code_issues_from_artifacts,
        task,
        status_code,
        offset=offset,
        limit=limit,
        tolerate_incomplete=task.status == "running",
    )
    return SimulationResponseCodeIssuePage(
        task_id=task_id,
        status_code=status_code,
        items=issue_slice.items,
        total=issue_slice.total,
        offset=offset,
        limit=limit,
    )


async def resume_queued_tasks() -> None:
    """Restore persisted queued tasks in creation order after a service restart."""
    task_ids = await asyncio.to_thread(_repository_list_ids)
    task_ids.extend(task_id for task_id in _legacy_task_ids() if task_id not in set(task_ids))
    for task_id in task_ids:
        await load_task(task_id)


def _empty_task_page(offset: int, limit: int) -> SimulationTaskListPage:
    return SimulationTaskListPage(
        tasks=[],
        total=0,
        offset=offset,
        limit=limit,
        counts={
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        },
        facets={
            "backends": [],
            "models": [],
            "datasets": [],
            "trace_formats": [],
            "endpoints": [],
        },
    )


async def list_tasks(
    *,
    status: SimulationTaskStatus | None = None,
    scenario: SimulationScenario | None = None,
    search: str | None = None,
    backend: BackendName | None = None,
    model: str | None = None,
    dataset: str | None = None,
    trace_format: TraceFormat | None = None,
    endpoint: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    offset: int = 0,
    limit: int = 10,
) -> SimulationTaskListPage:
    task_ids = await asyncio.to_thread(_repository_list_ids)
    task_ids.extend(task_id for task_id in _legacy_task_ids() if task_id not in set(task_ids))
    if not task_ids:
        return _empty_task_page(offset, limit)
    loaded = await asyncio.gather(*(load_task(task_id) for task_id in task_ids))
    valid = [task for task in loaded if task is not None]
    counts: dict[SimulationTaskStatus, int] = {
        "queued": sum(task.status == "queued" for task in valid),
        "running": sum(task.status == "running" for task in valid),
        "completed": sum(task.status == "completed" for task in valid),
        "failed": sum(task.status == "failed" for task in valid),
        "cancelled": sum(task.status == "cancelled" for task in valid),
    }
    facets = {
        "backends": sorted({task.simulation.backend for task in valid}),
        "models": sorted({task.model_name for task in valid}),
        "datasets": sorted({task.prompt.dataset.name for task in valid if task.prompt.dataset.name}),
        "trace_formats": sorted({task.prompt.trace.format for task in valid}),
        "endpoints": sorted({task.endpoint_url for task in valid}),
    }

    def parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    normalized_search = search.lower() if search else None
    normalized_endpoint = endpoint.lower() if endpoint else None
    after_timestamp = parse_timestamp(created_after) if created_after else None
    before_timestamp = parse_timestamp(created_before) if created_before else None

    def matches(task: SimulationTask) -> bool:
        if status and task.status != status:
            return False
        if scenario and task.scenario != scenario:
            return False
        if backend and task.simulation.backend != backend:
            return False
        if model and task.model_name != model:
            return False
        if dataset and task.prompt.dataset.name != dataset:
            return False
        if trace_format and task.prompt.trace.format != trace_format:
            return False
        if normalized_endpoint and normalized_endpoint not in task.endpoint_url.lower():
            return False
        created = parse_timestamp(task.created_at)
        if after_timestamp and created < after_timestamp:
            return False
        if before_timestamp and created > before_timestamp:
            return False
        if not normalized_search:
            return True
        values = [
            task.id,
            task.name,
            task.description,
            task.model_name,
            task.endpoint_url,
            task.scenario,
            task.simulation.backend,
            task.prompt.trace.path,
            task.prompt.trace.format,
        ]
        return any(normalized_search in value.lower() for value in values)

    filtered = [task for task in valid if matches(task)]

    def sort_key(task: SimulationTask) -> tuple[float, str]:
        try:
            timestamp = datetime.fromisoformat(task.created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            timestamp = 0
        return timestamp, task.id

    filtered.sort(key=sort_key, reverse=True)
    summaries = []
    for task in filtered[offset : offset + limit]:
        summary = task.model_copy(deep=True)
        summary.logs.clear()
        if summary.result is not None:
            summary.result.artifacts.clear()
            summary.result.per_request.clear()
            summary.result.backend_metrics.clear()
        summaries.append(summary)
    live_tasks = [task for task in summaries if task.status == "running"]
    if live_tasks:
        live_summaries = await asyncio.gather(
            *(asyncio.to_thread(live_summary_from_artifacts, task) for task in live_tasks)
        )
        for task, live_summary in zip(live_tasks, live_summaries, strict=True):
            task.live_summary = live_summary
    return SimulationTaskListPage(
        tasks=summaries,
        total=len(filtered),
        offset=offset,
        limit=limit,
        counts=counts,
        facets=facets,
    )


@dataclass(frozen=True)
class ResolvedTaskEndpoint:
    """A reachable endpoint plus optional deployment display metadata."""

    url: str
    cluster_id: str | None = None
    cluster_name: str | None = None
    deployment_name: str | None = None
    namespace: str | None = None
    configuration_ids: tuple[str, ...] = ()


def _task_execution_id(task: SimulationTask) -> str | None:
    """Return a task's deployment execution id, upgrading legacy run/case records."""
    if task.endpoint_deployment_execution_id:
        return task.endpoint_deployment_execution_id
    from llm_d_bench.deploy.executions import resolve_legacy_execution_id

    return resolve_legacy_execution_id(task.endpoint_deployment_run_id, task.endpoint_deployment_case_id)


async def _cluster_gateway_endpoint(cluster_id: str | None) -> str | None:
    """The cluster's shared-Gateway base URL (no ``/v1``) for benchmark traffic.

    Gateway Mode deployments disable their own proxy, so an in-cluster harness
    reaches a deployment's EPP through the shared Gateway. Returns ``None`` when
    the cluster has no ready Gateway, so callers fall back to the deployment's
    stored endpoint.
    """
    if not cluster_id:
        return None
    try:
        from llm_d_bench.model_service.gateway_ops import GatewayOpsService

        base = await GatewayOpsService().cluster_gateway_base_url(cluster_id)
    except Exception:  # noqa: BLE001 - simulation must not fail on a gateway lookup
        return None
    return base.removesuffix("/v1") if base else None


async def _resolve_task_endpoint_url(
    *,
    endpoint_mode: str,
    endpoint_url: str,
    endpoint_deployment_execution_id: str | None,
    backend_name: str | None = None,
    backend_capabilities: dict | None = None,
    force_gateway: bool = False,
) -> ResolvedTaskEndpoint:
    """Resolve the reachable endpoint URL for a task.

    Deployment-bound tasks resolve to the deployment's in-cluster Service URL
    (``http://<service>.<namespace>.svc:<port>``) rather than a host-side
    ``kubectl port-forward`` tunnel: a port-forward to a Service pins its whole
    tunnel to a single backing pod for its lifetime, so any traffic generated
    through it never actually load-balances across replicas the way real
    clients see it. The backend instead runs the benchmark from inside a Pod
    in the deployment's namespace (see ``llm_d_bench.simulation.incluster``),
    which reaches this in-cluster URL like any other cluster-local client.
    External/in-cluster-endpoint tasks keep their provided URL unchanged.

    ``force_gateway`` is set for tasks that target a published Model Service:
    its HTTPRoute reaches the InferencePool directly regardless of what data
    plane the underlying deployment rendered at deploy time (it may even
    still be rendering its own, unrelated proxy).
    """
    url = endpoint_url.rstrip("/")
    if endpoint_mode != "deployment" or not endpoint_deployment_execution_id:
        return ResolvedTaskEndpoint(url=url)

    from llm_d_bench.cluster.registry import get_cluster
    from llm_d_bench.deploy.executions import DeploymentExecutionNotFoundError, get_execution_context

    try:
        context = get_execution_context(endpoint_deployment_execution_id)
    except DeploymentExecutionNotFoundError as error:
        raise SimulationConfigurationError(str(error)) from error
    incompatible_guides = (backend_capabilities or {}).get("incompatible_deployment_guides") or []
    context_guide = getattr(context, "guide", None)
    if context_guide and context_guide in incompatible_guides:
        raise SimulationConfigurationError(
            f"Backend '{backend_name}' does not support the '{context_guide}' deployment guide: "
            "its prefill/decode routing forces max_tokens=1 on the prefill sub-request, which is "
            "incompatible with requests that also set min_tokens (as this backend always does). "
            "Pick a different backend or target a non-PD-disaggregation deployment."
        )
    if not context.endpoint:
        raise SimulationConfigurationError(
            "Deployment does not expose an in-cluster endpoint yet; wait for it to become ready"
        )
    if not context.namespace:
        raise SimulationConfigurationError("Deployment is missing its namespace; it may still be provisioning")
    cluster_name = None
    if context.cluster_id:
        try:
            cluster = get_cluster(context.cluster_id)
        except Exception:
            cluster = None
        cluster_name = cluster.name if cluster else None
    # Only deployments whose provider declared the shared Gateway as their data
    # plane (and that are not evaluation-owned) are reached through it; all other
    # deployments keep their own callable endpoint. A Model Service run always
    # forces it: its HTTPRoute reaches the InferencePool directly.
    endpoint_url = context.endpoint
    if force_gateway or getattr(context, "uses_shared_gateway", False):
        endpoint_url = await _cluster_gateway_endpoint(context.cluster_id) or context.endpoint
    return ResolvedTaskEndpoint(
        url=endpoint_url,
        cluster_id=context.cluster_id,
        cluster_name=cluster_name,
        deployment_name=context.display_name,
        namespace=context.namespace,
        configuration_ids=tuple(getattr(context, "configuration_artifact_ids", ())),
    )


async def create_task(request: SimulationTaskCreateRequest, *, http_request: object | None = None) -> SimulationTask:
    global pending_creates
    request = request.model_copy(deep=True)
    if request.model_service_group_id:
        from llm_d_bench.model_service.resolution import resolve_model_service_target  # noqa: PLC0415

        execution_id, published_name = resolve_model_service_target(request.model_service_group_id, http_request)
        request = request.model_copy(
            update={
                "endpoint_mode": "deployment",
                "endpoint_deployment_execution_id": execution_id,
                "model_name": published_name,
            }
        )
    async with _get_create_lock():
        pending_creates += 1
    try:
        descriptor = next(
            (candidate for candidate in list_backends() if candidate.name == request.backend),
            None,
        )
        if descriptor is None:
            raise SimulationConfigurationError(f"Unknown simulation backend '{request.backend}'")
        dataset = trace_registry.get(request.trace_dataset)
        if not any(item.name == request.scenario for item in descriptor.scenarios):
            raise SimulationConfigurationError(
                f"Backend '{request.backend}' does not support scenario '{request.scenario}'"
            )
        trace_formats = descriptor.capabilities.get("trace_formats")
        if not isinstance(trace_formats, list) or dataset["trace_format"] not in trace_formats:
            raise SimulationConfigurationError(
                f"Backend '{request.backend}' does not support dataset '{request.trace_dataset}'"
            )
        if dataset["scenario"] != request.scenario:
            raise SimulationConfigurationError(
                f"Dataset '{request.trace_dataset}' belongs to scenario '{dataset['scenario']}'"
            )
        trace_type = trace_registry.get_type(dataset["trace_format"])
        if trace_type.externally_managed:
            if request.trace_path != dataset["public_dataset"]:
                raise SimulationConfigurationError(
                    f"Dataset '{request.trace_dataset}' does not match the selected public dataset"
                )
        elif Path(request.trace_path).name != dataset["filename"]:
            raise SimulationConfigurationError(
                f"Dataset '{request.trace_dataset}' does not match the selected trace file"
            )
        capabilities = descriptor.capabilities
        format_capabilities = capabilities.get("trace_format_capabilities", {}).get(dataset["trace_format"], {})
        supports_scale_factor = format_capabilities.get(
            "supports_scale_factor",
            dataset["trace_format"] in capabilities.get("scale_factor_trace_formats", []),
        )
        if request.scale_factor != 1 and not supports_scale_factor:
            raise SimulationConfigurationError(
                f"Backend '{request.backend}' does not support scale_factor for {dataset['trace_format']} datasets"
            )
        range_start = request.trace_start_seconds
        range_end = request.trace_end_seconds
        supports_trace_range = format_capabilities.get(
            "supports_trace_range",
            capabilities.get("supports_trace_range", False),
        )
        trace_range_mode = format_capabilities.get(
            "trace_range_mode",
            "start_end" if supports_trace_range else "none",
        )
        if not supports_trace_range:
            if range_start != 0 or range_end is not None:
                raise SimulationConfigurationError(
                    f"Backend '{request.backend}' does not support trace start/end ranges"
                )
        elif trace_range_mode == "duration" and range_start != 0:
            raise SimulationConfigurationError(f"Backend '{request.backend}' requires trace_start_seconds to be 0")
        elif range_end is None and range_start > 0:
            raise SimulationConfigurationError(
                "trace_end_seconds is required when trace_start_seconds is greater than zero"
            )
        duration_seconds = request.duration_seconds
        if range_end is not None:
            await asyncio.to_thread(
                trace_registry.resolve_file(request.trace_path, dataset["trace_format"]).validate_range,
                range_start,
                range_end,
            )
            duration_seconds = max(1, math.ceil((range_end - range_start) / request.scale_factor))
        resolved_endpoint = await _resolve_task_endpoint_url(
            endpoint_mode=request.endpoint_mode,
            endpoint_url=request.endpoint_url,
            endpoint_deployment_execution_id=request.endpoint_deployment_execution_id,
            backend_name=request.backend,
            backend_capabilities=descriptor.capabilities,
            force_gateway=bool(request.model_service_group_id),
        )
        endpoint_url = resolved_endpoint.url
        while True:
            task_id = uuid.uuid4().hex[:8]
            if task_id not in tasks and not await asyncio.to_thread(_task_id_exists, task_id):
                break
        task = SimulationTask(
            id=task_id,
            name=request.name,
            description=request.description,
            scenario=request.scenario,
            status="queued",
            endpoint_mode=request.endpoint_mode,
            endpoint_namespace=request.endpoint_namespace or resolved_endpoint.namespace,
            endpoint_service=request.endpoint_service,
            endpoint_deployment_execution_id=request.endpoint_deployment_execution_id,
            endpoint_cluster_id=request.endpoint_cluster_id or resolved_endpoint.cluster_id,
            endpoint_cluster_name=request.endpoint_cluster_name or resolved_endpoint.cluster_name,
            endpoint_deployment_name=request.endpoint_deployment_name or resolved_endpoint.deployment_name,
            endpoint_url=endpoint_url,
            model_name=request.model_name,
            api_key=request.api_key,
            model_service_group_id=request.model_service_group_id,
            simulation=SimulationConfig(
                backend=request.backend,
                backend_options=request.backend_options,
                duration_seconds=duration_seconds,
                num_requests=None,
                stream=request.stream,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
            prompt=SimulationPrompt(
                type="trace",
                dataset=SimulationDataset(
                    name=request.trace_dataset,
                    scenario=dataset["scenario"],
                    tokenizer=None,
                ),
                trace=SimulationTrace(
                    path=request.trace_path,
                    format=dataset["trace_format"],
                    fixed_schedule=False if trace_type.externally_managed else request.fixed_schedule,
                    timeout_seconds=request.trace_timeout_seconds,
                    synthesis_speedup_ratio=request.scale_factor,
                    start_seconds=range_start,
                    end_seconds=range_end,
                ),
            ),
            task_dir=str(task_root() / task_id),
            configuration_ids=list(resolved_endpoint.configuration_ids),
            progress_percent=0,
            progress_message="Queued",
            logs=[],
            result=None,
            error_message=None,
            created_at=_now(),
            started_at=None,
            execution_started_at=None,
            completed_at=None,
            owner_pid=None,
            owner_instance_id=None,
        )
        backend_instance = get_backend(request.backend)
        await backend_instance.validate(task)
        tasks[task_id] = task
        await save_task(task)
    except Exception:
        if "task_id" in locals():
            tasks.pop(task_id, None)
        raise
    finally:
        async with _get_create_lock():
            pending_creates -= 1

    _schedule_task(task)
    return task


async def rerun_task(
    task_id: str,
    *,
    override: SimulationTaskRerunRequest | None = None,
    http_request: object | None = None,
) -> SimulationTask:
    global pending_creates
    source = await load_task(task_id)
    if source is None:
        raise SimulationConfigurationError("Simulation task not found")
    if source.status in {"queued", "running"}:
        raise SimulationConfigurationError("Cannot rerun an active simulation task")

    if source.prompt.dataset.name is None:
        raise SimulationConfigurationError("Simulation task does not reference a trace dataset")
    override = override or SimulationTaskRerunRequest()
    override_execution_id = override.endpoint_deployment_execution_id
    override_deployment_name = override.endpoint_deployment_name
    if override.model_service_group_id:
        from llm_d_bench.model_service.resolution import resolve_model_service_target  # noqa: PLC0415

        override_execution_id, override_deployment_name = resolve_model_service_target(
            override.model_service_group_id, http_request
        )
        model_service_group_id = override.model_service_group_id
    elif override.endpoint_deployment_execution_id:
        # An explicit raw-deployment override replaces whatever Model Service
        # the source task targeted.
        model_service_group_id = None
    else:
        # No override at all: keep targeting the same Model Service (if any).
        model_service_group_id = source.model_service_group_id
    execution_id = override_execution_id or _task_execution_id(source)
    cluster_id = override.endpoint_cluster_id or source.endpoint_cluster_id
    cluster_name = override.endpoint_cluster_name or source.endpoint_cluster_name
    deployment_name = override_deployment_name or source.endpoint_deployment_name
    base_url = override.endpoint_url or source.endpoint_url
    base_name = re.sub(r" · rerun [0-9a-f]{8}$", "", source.name)
    async with _get_create_lock():
        pending_creates += 1
    try:
        try:
            backend_capabilities = get_backend(source.simulation.backend).descriptor().capabilities
        except Exception:
            backend_capabilities = None
        resolved_endpoint = await _resolve_task_endpoint_url(
            endpoint_mode=source.endpoint_mode,
            endpoint_url=base_url,
            endpoint_deployment_execution_id=execution_id,
            backend_name=source.simulation.backend,
            backend_capabilities=backend_capabilities,
            force_gateway=bool(model_service_group_id),
        )
        endpoint_url = resolved_endpoint.url
        cluster_id = cluster_id or resolved_endpoint.cluster_id
        cluster_name = cluster_name or resolved_endpoint.cluster_name
        deployment_name = deployment_name or resolved_endpoint.deployment_name
        namespace = resolved_endpoint.namespace or source.endpoint_namespace
        while True:
            rerun_id = uuid.uuid4().hex[:8]
            if rerun_id not in tasks and not await asyncio.to_thread(_task_id_exists, rerun_id):
                break
        rerun = source.model_copy(
            update={
                "id": rerun_id,
                "endpoint_url": endpoint_url,
                "api_key": override.api_key,
                "endpoint_namespace": namespace,
                "endpoint_deployment_execution_id": execution_id,
                "endpoint_deployment_run_id": None,
                "endpoint_deployment_case_id": None,
                "endpoint_cluster_id": cluster_id,
                "endpoint_cluster_name": cluster_name,
                "endpoint_deployment_name": deployment_name,
                "model_service_group_id": model_service_group_id,
                "name": f"{base_name} · rerun {rerun_id}",
                "status": "queued",
                "task_dir": str(task_root() / rerun_id),
                "progress_percent": 0,
                "progress_message": "Queued",
                "logs": [],
                "logs_truncated": False,
                "artifacts_incomplete": False,
                "artifact_uri": None,
                "backend_version": None,
                "backend_version_inferred": False,
                "configuration_ids": list(resolved_endpoint.configuration_ids),
                "result": None,
                "error_message": None,
                "created_at": _now(),
                "started_at": None,
                "execution_started_at": None,
                "completed_at": None,
                "owner_pid": None,
                "owner_instance_id": None,
            },
            deep=True,
        )
        await get_backend(rerun.simulation.backend).validate(rerun)
        tasks[rerun.id] = rerun
        await save_task(rerun)
    except Exception:
        if "rerun" in locals():
            tasks.pop(rerun.id, None)
        raise
    finally:
        async with _get_create_lock():
            pending_creates -= 1

    _schedule_task(rerun)
    return rerun


async def _run_task(task: SimulationTask) -> None:
    cancel_event = asyncio.Event()
    controllers[task.id] = cancel_event

    def log(message: str) -> None:
        if not message:
            return
        task.logs.append(f"[{_now()}] {message}")
        maximum = _positive_environment("SIMULATION_MAX_TASK_LOG_LINES", 500)
        if len(task.logs) > maximum:
            task.logs_truncated = True
            del task.logs[: len(task.logs) - maximum]
        _queue_save(task)

    def progress(percent: float, message: str) -> None:
        task.progress_percent = max(0, min(100, percent))
        task.progress_message = message
        _queue_save(task)

    def mark_execution_started() -> None:
        # Fired once the backend tool (aiperf/trace-replayer) is actually
        # staged/installed and the real benchmark command begins, as opposed
        # to ``task.started_at`` which is set as soon as the task is picked
        # up (including pod launch/data staging/on-demand tool install). The
        # dashboard's elapsed-time clock is keyed off this instead so it
        # doesn't start ticking before the tool is actually ready.
        if task.execution_started_at is None:
            task.execution_started_at = _now()
            _queue_save(task)

    context = RunContext(cancel_event=cancel_event, log=log, progress=progress, on_ready=mark_execution_started)
    task.status = "running"
    task.owner_pid = os.getpid()
    task.owner_instance_id = service_instance_id
    task.started_at = _now()
    task.progress_message = "Simulation running"
    await save_task(task)
    try:
        resolved_endpoint = await _resolve_task_endpoint_url(
            endpoint_mode=task.endpoint_mode,
            endpoint_url=task.endpoint_url,
            endpoint_deployment_execution_id=_task_execution_id(task),
            force_gateway=bool(task.model_service_group_id),
        )
        task.endpoint_url = resolved_endpoint.url
        await save_task(task)
        task.configuration_ids = list(resolved_endpoint.configuration_ids)
        backend = get_backend(task.simulation.backend)
        task.backend_version = backend.descriptor().version
        task.result = await backend.run(task, context)
        task.status = "completed"
        task.progress_percent = 100
        task.progress_message = "Simulation completed"
    except (SimulationCancelledError, asyncio.CancelledError):
        task.status = "cancelled"
        task.progress_message = "Simulation cancelled"
    except Exception as error:
        task.status = "failed"
        task.progress_message = "Simulation failed"
        task.error_message = (
            str(error) if isinstance(error, SimulationError) else f"Unexpected simulation error: {error}"
        )
        if not isinstance(error, SimulationError):
            logger.exception("Unexpected simulation task failure %s", task.id)
    finally:
        task.completed_at = _now()
        task.artifacts_incomplete = context.artifacts_incomplete
        controllers.pop(task.id, None)
        await _await_saves(task.id)
        await save_task(task)


async def stop_task(task_id: str) -> None:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    if task.status not in {"queued", "running"}:
        return
    controller = controllers.get(task_id)
    if controller is not None:
        controller.set()
    else:
        queued_runner = runners.pop(task_id, None)
        if queued_runner is not None:
            queued_runner.cancel()
        task.status = "cancelled"
        task.completed_at = _now()
    task.progress_message = "Cancellation requested"
    await save_task(task)


async def delete_task(task_id: str) -> None:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    if task_id in runners or task.status in {"queued", "running"}:
        raise SimulationConfigurationError("Stop the simulation task before deleting it")
    await _await_saves(task_id)
    directory = task_root() / task_id
    if Path(task.task_dir).resolve() != directory or directory.is_symlink():
        raise SimulationConfigurationError("Simulation task directory is invalid")
    shutil.rmtree(directory)
    await asyncio.to_thread(_repository_delete, task_id)
    tasks.pop(task_id, None)
    controllers.pop(task_id, None)
    save_locks.pop(task_id, None)
    save_jobs.pop(task_id, None)


async def shutdown_service() -> None:
    """Cancel active simulations and wait for their subprocesses to terminate."""
    queued = [
        runner
        for task_id, runner in list(runners.items())
        if (task := tasks.get(task_id)) is not None and task.status == "queued"
    ]
    for runner in queued:
        runner.cancel()
    if queued:
        await asyncio.gather(*queued, return_exceptions=True)
    for event in controllers.values():
        event.set()
    active = [runner for runner in runners.values() if runner not in queued]
    if not active:
        return
    done, pending = await asyncio.wait(active, timeout=10)
    for runner in pending:
        runner.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.gather(*done, return_exceptions=True)


def reset_service_state() -> None:
    """Reset process-local state for focused tests."""
    global pending_creates, create_lock, persist_lock, run_semaphore, _dao
    for runner in runners.values():
        runner.cancel()
    tasks.clear()
    controllers.clear()
    runners.clear()
    save_locks.clear()
    save_jobs.clear()
    pending_creates = 0
    create_lock = None
    persist_lock = None
    run_semaphore = None
    _dao = None


def _task_id_exists(task_id: str) -> bool:
    if _repository_get(task_id) is not None:
        return True
    return (task_root() / task_id).exists()


def _legacy_task_ids() -> list[str]:
    try:
        return [
            entry.name
            for entry in task_root().iterdir()
            if entry.is_dir() and TASK_ID_PATTERN.fullmatch(entry.name) and (entry / "task.json").is_file()
        ]
    except FileNotFoundError:
        return []


def _load_legacy_task(task_id: str) -> SimulationTask | None:
    path = task_root() / task_id / "task.json"
    try:
        return SimulationTask.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _persist_task(task: SimulationTask) -> None:
    directory = Path(task.task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    per_request_payload = (
        [item.model_dump(mode="json") for item in task.result.per_request]
        if task.result and task.result.per_request
        else None
    )
    _persist_optional_json(
        directory / PER_REQUEST_FILENAME,
        per_request_payload,
    )
    _persist_optional_json(
        directory / RESULT_EXTRA_FILENAME,
        dict(task.result.model_extra or {}) if task.result is not None and task.result.model_extra else None,
    )
    _repository_save(task)


def _persist_optional_json(path: Path, payload: object | None) -> None:
    if payload is None:
        path.unlink(missing_ok=True)
        return
    temporary = Path(f"{path}.{uuid.uuid4()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _repository_save(task: SimulationTask) -> SimulationTask:
    with _repository_thread_lock:
        return _task_dao().save(task)


def _repository_get(task_id: str) -> SimulationTask | None:
    with _repository_thread_lock:
        return _task_dao().get(task_id)


def _repository_list_ids() -> list[str]:
    with _repository_thread_lock:
        return _task_dao().list_ids()


def _repository_delete(task_id: str) -> None:
    with _repository_thread_lock:
        _task_dao().delete(task_id)
