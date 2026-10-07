"""Execution-scoped view of Deploy-owned deployments.

``execution_id`` is the only deployment identifier Deploy exposes to other
modules. Simulation, Monitoring, and the frontend reference a deployment solely
by that id; the ``run_id``/``case_id`` pair stays inside Deploy where it belongs
to run orchestration and audit.

This module owns the reverse lookup from an execution to its owning run/case and
the projection of a deployment into the facts other modules need (namespace,
cluster, endpoints, serving metadata).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from llm_d_bench.deploy.application import deployment_run_manager
from llm_d_bench.deploy.contracts import (
    DeploymentMetadata,
    DeploymentMetadataUpdateRequest,
    DeploymentStatus,
)
from llm_d_bench.deploy.data_plane import deployment_uses_shared_gateway
from llm_d_bench.deploy.runtime.composition import RuntimeConfigurationError
from llm_d_bench.deploy.usage import active_usage_reason

_store = deployment_run_manager.store
# Serializes user-driven mutations of one deployment so a metadata edit and a
# delete never interleave their read-modify-write cycles.
_execution_locks: dict[str, asyncio.Lock] = {}


class DeploymentExecutionNotFoundError(Exception):
    """Raised when no deployment execution matches the requested id."""


class DeploymentExecutionConflictError(Exception):
    """Raised when a deployment cannot be changed in its current state."""

    def __init__(self, message: str, code: str = "deployment_conflict") -> None:
        super().__init__(message)
        self.code = code


def _execution_lock(execution_id: str) -> asyncio.Lock:
    lock = _execution_locks.get(execution_id)
    if lock is None:
        lock = asyncio.Lock()
        _execution_locks[execution_id] = lock
    return lock


@dataclass(frozen=True)
class DeploymentExecutionContext:
    """Deployment facts other modules may consume, keyed by ``execution_id``.

    ``run_id`` and ``case_id`` are carried for Deploy-internal callers only and
    are deliberately excluded from the HTTP projection.
    """

    execution_id: str
    run_id: str
    case_id: str
    status: str
    cluster_id: str | None
    cluster_session_id: str | None
    namespace: str | None
    name: str | None
    display_name: str
    description: str
    model: str | None
    backend: str | None
    guide: str | None
    endpoint: str | None
    forwarded_endpoint: str | None
    replicas: int | None
    tensor_parallel_size: int | None
    image: str | None
    preserve_deployment: bool
    created_at: Any
    updated_at: Any = None
    max_model_len: int | None = None
    gpu_memory_utilization: Any = None
    enable_prefix_caching: Any = None
    max_num_seqs: Any = None
    max_num_batched_tokens: Any = None
    storage_type: str | None = None
    storage_volume_id: str | None = None
    mount_path: str | None = None
    pvc_name: str | None = None
    failure: dict[str, Any] | None = None
    custom_parameters: list[dict[str, Any]] | None = None
    baseline_endpoint: str | None = None
    service_ref: str | None = None
    #: True when an evaluation created this ephemeral deployment for benchmarking.
    evaluate_owned: bool = False
    #: True when a benchmark must reach this deployment through the shared Gateway.
    uses_shared_gateway: bool = False
    configuration_artifact_ids: tuple[str, ...] = ()

    def api_payload(self) -> dict[str, Any]:
        """Project the execution for HTTP clients without leaking run/case ids."""
        created_at = self.created_at
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "cluster_id": self.cluster_id,
            "cluster_session_id": self.cluster_session_id,
            "namespace": self.namespace,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "model": self.model,
            "backend": self.backend,
            "guide": self.guide,
            "endpoint": self.endpoint,
            "forwarded_endpoint": self.forwarded_endpoint,
            "replicas": self.replicas,
            "tensor_parallel_size": self.tensor_parallel_size,
            "image": self.image,
            "preserve_deployment": self.preserve_deployment,
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "updated_at": _isoformat(self.updated_at),
            "max_model_len": self.max_model_len,
            "gpu_memory_utilization": self.gpu_memory_utilization,
            "enable_prefix_caching": self.enable_prefix_caching,
            "max_num_seqs": self.max_num_seqs,
            "max_num_batched_tokens": self.max_num_batched_tokens,
            "storage_type": self.storage_type,
            "storage_volume_id": self.storage_volume_id,
            "mount_path": self.mount_path,
            "pvc_name": self.pvc_name,
            "failure": self.failure,
            "custom_parameters": self.custom_parameters or [],
            "baseline_endpoint": self.baseline_endpoint,
            "service_ref": self.service_ref,
        }

    def search_haystack(self) -> str:
        """Lower-cased text a deployment can be searched by."""
        return " ".join(
            value.lower()
            for value in (
                self.execution_id,
                self.display_name,
                self.description,
                self.name,
                self.model,
                self.namespace,
                self.guide,
                self.backend,
                self.status,
            )
            if value
        )


def _isoformat(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def serving_metadata(case, execution) -> dict[str, Any]:
    """Read serving details out of a case's rendered configuration artifact.

    Shared by the execution projection and the deployment payloads returned by
    the run/case routes so a deployment reports the same serving facts wherever
    it is observed.
    """
    content: Any = {}
    if case and case.create_request.configuration_artifacts:
        try:
            content = json.loads(case.create_request.configuration_artifacts[0].content or "{}")
        except json.JSONDecodeError:
            content = {}
    model = content.get("model") if isinstance(content, dict) else {}
    decode = content.get("decode") if isinstance(content, dict) else {}
    runtime = content.get("runtime") if isinstance(content, dict) else {}
    custom_params = (
        content.get("customParameters")
        if isinstance(content, dict) and isinstance(content.get("customParameters"), list)
        else []
    )

    params_map = {}
    for p in custom_params:
        if isinstance(p, dict) and p.get("name"):
            params_map[p["name"]] = p.get("value")

    manifest_ref = str(execution.artifact.manifest_ref or "") if execution else ""
    provider_ref = case.provider_ref if case else ""
    policy = case.create_request.deployment_policy.value if case else {}
    deployment_name = str((policy or {}).get("deployment_name") or "") or provider_ref
    backend = (
        "sglang"
        if "sglang" in manifest_ref or "sglang" in provider_ref
        else "vLLM"
        if "vllm" in manifest_ref or provider_ref == "optimized-baseline"
        else None
    )
    storage_type = (
        runtime.get("storageType")
        or (
            "model-cache"
            if "storageVolumeId" in runtime
            else "pvc"
            if "pvcName" in runtime
            else "local-cache"
            if "mountPath" in runtime
            else None
        )
        if isinstance(runtime, dict)
        else None
    )
    return {
        "name": deployment_name or None,
        "guide": provider_ref or None,
        "backend": backend,
        "model_name": model.get("name") if isinstance(model, dict) else None,
        "replicas": decode.get("replicaCount") if isinstance(decode, dict) else None,
        "tensor_parallel_size": decode.get("tensorParallelSize") if isinstance(decode, dict) else None,
        "max_model_len": decode.get("maxModelLen") if isinstance(decode, dict) else None,
        "image": runtime.get("image") if isinstance(runtime, dict) else None,
        "storage_type": storage_type,
        "storage_volume_id": runtime.get("storageVolumeId") if isinstance(runtime, dict) else None,
        "mount_path": runtime.get("mountPath") if isinstance(runtime, dict) else None,
        "pvc_name": runtime.get("pvcName") if isinstance(runtime, dict) else None,
        "gpu_memory_utilization": params_map.get("gpu-memory-utilization"),
        "enable_prefix_caching": params_map.get("enable-prefix-caching"),
        "max_num_seqs": params_map.get("max-num-seqs"),
        "max_num_batched_tokens": params_map.get("max-num-batched-tokens"),
        "custom_parameters": custom_params,
    }


def _context(run, case, execution) -> DeploymentExecutionContext:
    serving = serving_metadata(case, execution)
    metadata = getattr(execution, "metadata", None) or DeploymentMetadata()
    exec_prov = getattr(execution, "provenance", {}) or {}
    run_prov = getattr(run, "provenance", {}) or {}
    cluster_id = _optional_str(exec_prov.get("cluster_server_id")) or _optional_str(run_prov.get("cluster_server_id"))
    cluster_session_id = _optional_str(exec_prov.get("cluster_session_id")) or _optional_str(
        run_prov.get("cluster_session_id")
    )

    failure_obj = getattr(case, "failure", None)
    failure_dict = None
    if failure_obj:
        failure_dict = {
            "title": getattr(failure_obj, "title", None),
            "detail": getattr(failure_obj, "detail", None),
            "code": getattr(failure_obj, "code", None),
            "status": getattr(failure_obj, "status", None),
        }

    endpoint_obj = getattr(execution, "endpoint", None)
    baseline_endpoint = getattr(endpoint_obj, "baseline_url", None) if endpoint_obj else None
    service_ref = getattr(endpoint_obj, "service_ref", None) if endpoint_obj else None

    return DeploymentExecutionContext(
        configuration_artifact_ids=tuple(
            getattr(getattr(execution, "artifact", None), "configuration_artifact_ids", ())
        ),
        execution_id=execution.execution_id,
        run_id=run.id if run else "",
        case_id=case.id if case else "",
        status=execution.status.value if hasattr(execution.status, "value") else str(execution.status),
        cluster_id=cluster_id,
        cluster_session_id=cluster_session_id,
        namespace=execution.namespace,
        name=serving["name"],
        display_name=metadata.display_name,
        description=metadata.description,
        model=serving["model_name"],
        backend=serving["backend"],
        guide=serving["guide"],
        endpoint=execution.endpoint.url if execution.endpoint else None,
        forwarded_endpoint=execution.forwarded_endpoint,
        replicas=serving["replicas"],
        tensor_parallel_size=serving["tensor_parallel_size"],
        image=serving["image"],
        preserve_deployment=bool(execution.provenance.get("preserve_deployment")),
        created_at=getattr(execution, "created_at", None) or getattr(run, "created_at", None),
        updated_at=getattr(execution, "updated_at", None),
        max_model_len=serving["max_model_len"],
        gpu_memory_utilization=serving["gpu_memory_utilization"],
        enable_prefix_caching=serving["enable_prefix_caching"],
        max_num_seqs=serving["max_num_seqs"],
        max_num_batched_tokens=serving["max_num_batched_tokens"],
        storage_type=serving["storage_type"],
        storage_volume_id=serving["storage_volume_id"],
        mount_path=serving["mount_path"],
        pvc_name=serving["pvc_name"],
        failure=failure_dict,
        custom_parameters=serving["custom_parameters"],
        baseline_endpoint=baseline_endpoint,
        service_ref=service_ref,
        evaluate_owned=bool(
            exec_prov.get("evaluate_workflow")
            or exec_prov.get("evaluation_id")
            or exec_prov.get("evaluation_case_id")
            or run_prov.get("evaluate_workflow")
            or run_prov.get("evaluation_id")
        ),
        uses_shared_gateway=deployment_uses_shared_gateway(execution),
    )


def _optional_str(value: object | None) -> str | None:
    return str(value) if value else None


def list_execution_contexts(
    *,
    status: DeploymentStatus | None = None,
    statuses: list[DeploymentStatus] | None = None,
    cluster_id: str = "",
    query: str = "",
    limit: int | None = None,
    allowed_cluster_ids: set[str] | None = None,
) -> list[DeploymentExecutionContext]:
    """List deployment executions without creating endpoint port-forwards."""
    wanted = {item for item in (statuses or []) if item is not None}
    if status is not None:
        wanted.add(status)
    needle = query.strip().lower()
    contexts: list[DeploymentExecutionContext] = []
    executions_by_request_id = {}
    if hasattr(_store, "list_executions"):
        try:
            executions_by_request_id = {
                e.request_id: e for e in _store.list_executions() if getattr(e, "request_id", None)
            }
        except Exception:
            executions_by_request_id = {}

    for run in reversed(_store.list_runs()):
        if cluster_id and str(run.provenance.get("cluster_server_id") or "") != cluster_id:
            continue
        if (
            allowed_cluster_ids is not None
            and str(run.provenance.get("cluster_server_id") or "") not in allowed_cluster_ids
        ):
            continue
        for case in run.cases:
            if (
                not case.execution_id
                and case.create_request
                and case.create_request.request_id in executions_by_request_id
            ):
                case.execution_id = executions_by_request_id[case.create_request.request_id].execution_id
            if not case.execution_id:
                continue
            execution = _store.get_execution(case.execution_id)
            if execution is None or (wanted and execution.status not in wanted):
                continue
            context = _context(run, case, execution)
            if needle and needle not in context.search_haystack():
                continue
            contexts.append(context)
    contexts.sort(key=lambda context: context.execution_id)
    contexts.sort(key=lambda context: str(context.created_at or ""), reverse=True)
    return contexts[:limit] if limit is not None else contexts


def find_execution_context(execution_id: str) -> DeploymentExecutionContext | None:
    """Resolve one deployment execution and its owning run/case."""
    execution = _store.get_execution(execution_id)
    if execution is None and execution_id.startswith("pending:"):
        parts = execution_id.split(":", 2)
        if len(parts) == 3:
            run_id, case_id = parts[1], parts[2]
            case = _store.get_case(run_id, case_id)
            if case:
                if case.execution_id:
                    execution = _store.get_execution(case.execution_id)
                    if execution:
                        execution_id = case.execution_id
                if execution is None and case.create_request and hasattr(_store, "list_executions"):
                    req_id = case.create_request.request_id
                    try:
                        execution = next(
                            (e for e in _store.list_executions() if getattr(e, "request_id", None) == req_id), None
                        )
                    except Exception:
                        execution = None
                    if execution:
                        case.execution_id = execution.execution_id
                        execution_id = execution.execution_id

    if execution is not None:
        for run in reversed(_store.list_runs()):
            for case in run.cases:
                if case.execution_id == execution_id or (
                    case.create_request and case.create_request.request_id == execution.request_id
                ):
                    if not case.execution_id:
                        case.execution_id = execution.execution_id
                    return _context(run, case, execution)
        serving = serving_metadata(None, execution)
        metadata = getattr(execution, "metadata", None) or DeploymentMetadata()
        return DeploymentExecutionContext(
            configuration_artifact_ids=tuple(
                getattr(getattr(execution, "artifact", None), "configuration_artifact_ids", ())
            ),
            execution_id=execution.execution_id,
            run_id="",
            case_id="",
            status=execution.status.value if hasattr(execution.status, "value") else str(execution.status),
            cluster_id=_optional_str(execution.provenance.get("cluster_server_id")),
            cluster_session_id=_optional_str(execution.provenance.get("cluster_session_id")),
            namespace=execution.namespace,
            name=serving["name"],
            display_name=metadata.display_name,
            description=metadata.description,
            model=serving["model_name"],
            backend=serving["backend"],
            guide=serving["guide"],
            endpoint=execution.endpoint.url if execution.endpoint else None,
            forwarded_endpoint=execution.forwarded_endpoint,
            replicas=serving["replicas"],
            tensor_parallel_size=serving["tensor_parallel_size"],
            image=serving["image"],
            preserve_deployment=bool(execution.provenance.get("preserve_deployment")),
            created_at=getattr(execution, "created_at", None),
            updated_at=getattr(execution, "updated_at", None),
        )

    if execution_id.startswith("pending:"):
        parts = execution_id.split(":", 2)
        if len(parts) == 3:
            run_id, case_id = parts[1], parts[2]
            run = _store.get_run(run_id)
            case = _store.get_case(run_id, case_id)
            if run and case:
                provenance = case.create_request.provenance or run.provenance or {}
                serving = serving_metadata(case, None)
                metadata = DeploymentMetadata(description=str(provenance.get("description") or "").strip())
                return DeploymentExecutionContext(
                    execution_id=execution_id,
                    run_id=run.id,
                    case_id=case.id,
                    status=case.status.value if hasattr(case.status, "value") else str(case.status),
                    cluster_id=_optional_str(provenance.get("cluster_server_id")),
                    cluster_session_id=_optional_str(provenance.get("cluster_session_id")),
                    namespace=_optional_str(provenance.get("deployment_namespace")),
                    name=serving["name"] or provenance.get("deployment_name"),
                    display_name=metadata.display_name or str(provenance.get("deployment_name") or ""),
                    description=metadata.description,
                    model=serving["model_name"],
                    backend=serving["backend"],
                    guide=serving["guide"],
                    endpoint=None,
                    forwarded_endpoint=None,
                    replicas=serving["replicas"],
                    tensor_parallel_size=serving["tensor_parallel_size"],
                    image=serving["image"],
                    preserve_deployment=bool(provenance.get("preserve_deployment")),
                    created_at=getattr(run, "created_at", None),
                    updated_at=getattr(run, "finished_at", None),
                )

    return None


def resolve_legacy_execution_id(run_id: str | None, case_id: str | None) -> str | None:
    """Translate a historical ``run_id``/``case_id`` pair into an execution id.

    Only for reading records persisted before ``execution_id`` became the sole
    cross-module deployment reference.
    """
    if not run_id or not case_id:
        return None
    case = _store.get_case(run_id, case_id)
    return case.execution_id if case else None


_TERMINAL_STATUSES = {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}


def list_storage_volume_usage(volume_id: str) -> list[str]:
    """Return execution ids of non-terminal executions referencing a storage volume.

    Used by the Storage module to reject deleting a volume that is still
    mounted by a deployment. This is a read-only cross-module query (the
    inverse of ``deploy.usage``'s probe registry: here Storage queries Deploy
    directly since Deploy already depends on Storage for mount resolution).
    """
    matches: list[str] = []
    for run in _store.list_runs():
        for case in run.cases:
            if not case.execution_id:
                continue
            execution = _store.get_execution(case.execution_id)
            if execution is None or execution.status in _TERMINAL_STATUSES:
                continue
            runtime = _case_runtime(case)
            if str(runtime.get("storageVolumeId") or "") == volume_id:
                matches.append(case.execution_id)
    return matches


def _case_runtime(case) -> dict[str, Any]:
    if not case.create_request.configuration_artifacts:
        return {}
    try:
        content = json.loads(case.create_request.configuration_artifacts[0].content or "{}")
    except json.JSONDecodeError:
        return {}
    runtime = content.get("runtime") if isinstance(content, dict) else None
    return runtime if isinstance(runtime, dict) else {}


def get_execution_context(execution_id: str) -> DeploymentExecutionContext:
    """Resolve a deployment execution or raise :class:`DeploymentExecutionNotFoundError`.

    This is the supported entry point for other backend modules (Monitoring,
    Simulation) that need deployment facts. Callers must not read the Deploy run
    store directly.
    """
    context = find_execution_context(execution_id)
    if context is None:
        raise DeploymentExecutionNotFoundError(f"deployment execution not found: {execution_id}")
    return context


async def update_execution_metadata(
    execution_id: str, request: DeploymentMetadataUpdateRequest
) -> DeploymentExecutionContext:
    """Edit only a deployment's descriptive metadata.

    Runtime facts (model, namespace, image, topology, endpoint, cluster, status)
    are produced by Deploy and stay immutable here.
    """
    async with _execution_lock(execution_id):
        get_execution_context(execution_id)
        _store.update_execution_metadata(execution_id, request)
        return get_execution_context(execution_id)


async def delete_execution(execution_id: str, *, delete_namespace: bool = True) -> None:
    """Remove a deployment's Prism records, optionally cleaning its cluster resources first.

    A deployment still used by another module is never destroyed, and cleanup
    failures keep the records so the delete can be retried. When
    ``delete_namespace`` is false, the Kubernetes namespace and its workloads
    are left running and only Prism's own records are dropped.
    """
    async with _execution_lock(execution_id):
        context = get_execution_context(execution_id)
        reason = active_usage_reason(execution_id)
        if reason:
            raise DeploymentExecutionConflictError(reason, code="deployment_in_use")
        try:
            worker = deployment_run_manager.worker_for_run(context.run_id)
            await worker.delete_case_execution(context.run_id, context.case_id, delete_namespace=delete_namespace)
        except RuntimeConfigurationError as error:
            raise DeploymentExecutionConflictError(
                f"deployment runtime is unavailable, so cluster resources cannot be cleaned: {error}",
                code="deployment_runtime_unavailable",
            ) from error
        except ValueError as error:
            raise DeploymentExecutionConflictError(str(error)) from error
    _execution_locks.pop(execution_id, None)


async def ensure_execution_endpoint(execution_id: str):
    """Ensure a host-reachable endpoint exists for a deployment execution.

    Reuses a healthy port-forward or rebuilds a dead one, then returns the
    resolved endpoint. Imported lazily so this module stays importable from the
    endpoint resolver itself.
    """
    from llm_d_bench.deploy.endpoint import resolve_deployment_endpoint

    context = get_execution_context(execution_id)
    return await resolve_deployment_endpoint(context.run_id, context.case_id)
