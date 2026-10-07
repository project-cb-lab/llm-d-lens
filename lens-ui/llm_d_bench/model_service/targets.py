"""Derive a model-service member's routing target from a deployment execution.

The publish form only needs the cluster + execution; namespace/service/port/
kind are read from the persisted execution (never trusted from the request
body). Reuses the same fields ``deploy/endpoint.py`` parses.

Design reference: docs/design/model-service-gateway-deployment.md section 4.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from llm_d_bench.deploy.application import deployment_run_manager


class ExecutionTargetError(Exception):
    """The execution cannot be published (missing endpoint/namespace/cluster)."""


class ExecutionMissingError(ExecutionTargetError):
    """The execution record no longer exists (its deployment was deleted)."""


@dataclass(frozen=True)
class ExecutionTarget:
    execution_id: str
    cluster_id: str
    namespace: str
    service: str
    port: int
    endpoint_kind: str
    model_ref: str | None
    status: str
    display_name: str
    name: str | None
    model: str | None


def _endpoint_kind(service: str, baseline_url: str | None) -> str:
    if service.endswith("-epp") or baseline_url:
        return "llm-d-epp"
    return "vllm"


def resolve_execution_target(execution_id: str) -> ExecutionTarget:
    """Read the execution and return its publishable target.

    Raises ``ExecutionTargetError`` when the execution is unknown, has no ready
    endpoint, or is missing namespace/cluster/service/port metadata.
    """
    execution = deployment_run_manager.store.get_execution(execution_id)
    if execution is None:
        raise ExecutionMissingError(f"execution not found: {execution_id}")
    if execution.endpoint is None:
        raise ExecutionTargetError(f"execution has no ready endpoint: {execution_id}")

    parsed = urlparse(execution.endpoint.url)
    service = (parsed.hostname or "").split(".", 1)[0]
    port = parsed.port
    cluster_id = (execution.provenance or {}).get("cluster_server_id")
    if not service or not port or not execution.namespace or not cluster_id:
        raise ExecutionTargetError(f"execution is missing namespace/cluster/service/port: {execution_id}")
    return ExecutionTarget(
        execution_id=execution.execution_id,
        cluster_id=str(cluster_id),
        namespace=execution.namespace,
        service=service,
        port=int(port),
        endpoint_kind=_endpoint_kind(service, execution.endpoint.baseline_url),
        model_ref=execution.endpoint.model_ref,
        status=str(execution.status),
        display_name=_display_name(execution),
        name=_serving_name(execution.execution_id),
        model=(_serving_metadata_for(execution.execution_id).get("model_name") or execution.endpoint.model_ref),
    )


def execution_name(execution_id: str) -> str | None:
    """The deployment's internal name for display (e.g. ``qwen-qwen3-0-6b-...``)."""
    return _serving_name(execution_id)


def _display_name(execution) -> str:
    metadata = getattr(execution, "metadata", None)
    name = (getattr(metadata, "display_name", "") or "").strip()
    if name:
        return name
    endpoint = getattr(execution, "endpoint", None)
    return (getattr(endpoint, "model_ref", None) or "").strip()


def _serving_metadata_for(execution_id: str) -> dict:
    """The deployment's ``serving_metadata`` (name/model_name/...) or ``{}``.

    Read via the same projection the deployment pages use; the values live on
    the case's deployment policy, so find the owning case.
    """
    from llm_d_bench.deploy.executions import serving_metadata  # noqa: PLC0415

    for run in deployment_run_manager.store.list_runs():
        for case in run.cases:
            if case.execution_id != execution_id:
                continue
            execution = deployment_run_manager.store.get_execution(execution_id)
            return serving_metadata(case, execution) or {}
    return {}


def _serving_name(execution_id: str) -> str | None:
    """The deployment's internal name (e.g. ``qwen-qwen3-0-6b-202609210558``)."""
    return _serving_metadata_for(execution_id).get("name")
