"""Shared logic for resolving a deployment's reachable (port-forwarded) endpoint.

Both the deployments HTTP router and the simulation service need to translate a
deployment case into a host-reachable URL. The endpoint URL stored on a
``DeploymentExecution`` is a Kubernetes-internal ClusterIP address that benchmark
processes running on the host cannot reach; only a ``kubectl port-forward`` tunnel
to ``127.0.0.1`` works. This module keeps that translation in one place so that
simulation can re-resolve a fresh tunnel when the previous one dies (for example
after the backend restarts and the port-forward child process is reaped).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from llm_d_bench.cluster.registry import get_cluster
from llm_d_bench.deploy.application import deployment_run_manager
from llm_d_bench.deploy.contracts import utcnow
from llm_d_bench.utils.kubernetes import ensure_port_forward

_store = deployment_run_manager.store


class DeploymentEndpointResolutionError(Exception):
    """Raised when a deployment endpoint cannot be resolved to a local URL."""


@dataclass(frozen=True)
class ResolvedDeploymentEndpoint:
    """A freshly (re)established local port-forward for a deployment case."""

    endpoint: str
    forward_id: str
    local_port: int
    namespace: str
    service: str
    remote_port: int
    cluster_id: str | None = None
    cluster_name: str | None = None
    deployment_name: str | None = None


async def resolve_deployment_endpoint(run_id: str, case_id: str) -> ResolvedDeploymentEndpoint:
    """Resolve the latest local endpoint for a deployment case.

    Reuses an existing healthy port-forward, or starts a new one if the old tunnel
    died. Persists the refreshed ``forwarded_endpoint`` back onto the execution so
    callers (and the list API) always observe the current mapping.

    Raises:
        DeploymentEndpointResolutionError: the case/endpoint cannot be found or is
            missing namespace/service/port metadata.
        FileNotFoundError: ``kubectl`` is unavailable.
        PortForwardError: the tunnel could not be established.
    """
    case = _store.get_case(run_id, case_id)
    if case is None:
        raise DeploymentEndpointResolutionError("deployment case not found")
    execution = _store.get_execution(case.execution_id) if case.execution_id else None
    if execution is None or execution.endpoint is None:
        raise DeploymentEndpointResolutionError("deployment endpoint is not available")

    namespace = execution.namespace
    parsed = urlparse(execution.endpoint.url)
    service = (parsed.hostname or "").split(".", 1)[0]
    remote_port = parsed.port
    cluster_id = execution.provenance.get("cluster_server_id")
    if not namespace or not service or not remote_port:
        raise DeploymentEndpointResolutionError("deployment endpoint is missing namespace, service, or port")

    forward = await ensure_port_forward(
        namespace,
        service,
        remote_port,
        cluster_id=str(cluster_id) if cluster_id else None,
    )

    forwarded_endpoint = f"http://127.0.0.1:{forward.local_port}"
    execution.forwarded_endpoint = forwarded_endpoint
    execution.updated_at = utcnow()
    _store.save_execution(execution)

    return ResolvedDeploymentEndpoint(
        endpoint=forwarded_endpoint,
        forward_id=forward.id,
        local_port=forward.local_port,
        namespace=namespace,
        service=service,
        remote_port=remote_port,
        cluster_id=str(cluster_id) if cluster_id else None,
        cluster_name=_cluster_name(cluster_id),
        deployment_name=_deployment_name(case),
    )


def _cluster_name(cluster_id: object | None) -> str | None:
    if not cluster_id:
        return None
    try:
        cluster = get_cluster(str(cluster_id))
    except Exception:
        return None
    return cluster.name if cluster else None


def _deployment_name(case) -> str | None:
    policy = None
    if getattr(case, "create_request", None) is not None:
        policy = case.create_request.deployment_policy
    policy_value = policy.value if policy is not None else {}
    return str((policy_value or {}).get("deployment_name") or "") or (case.provider_ref or None)
