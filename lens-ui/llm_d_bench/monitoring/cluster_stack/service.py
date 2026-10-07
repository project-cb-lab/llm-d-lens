"""Use-case orchestration for cluster monitoring stack management."""

from __future__ import annotations

from llm_d_bench.monitoring.service_links import (
    service_name as _service_name, service_port as _service_port, find_service as _find_service,
)

import json
import os
import re
from pathlib import Path

from llm_d_bench.utils.kubernetes import PortForwardError, ensure_port_forward, kubeconfig_environment, scoped_runner
from llm_d_bench.utils.kubernetes import validate_namespace as validate_namespace
from llm_d_bench.utils.shell import CommandRunner, ScopedCommandRunner

from .discovery import discover_cluster_stack
from .errors import ClusterStackError
from .models import (
    ClusterStackInstallRequest,
    ClusterStackLinksResponse,
    ClusterStackOperationResponse,
    ClusterStackPreflightResponse,
    ClusterStackStatusResponse,
    ObservabilityLink,
    PreflightCheck,
)
from .operations import ClusterStackOperationManager
from .repo import repo_root

runner = CommandRunner()
operations = ClusterStackOperationManager()


def _runner(cluster_id: str | None) -> CommandRunner:
    """Return a runner scoped to a cluster's kubeconfig (or the ambient runner)."""
    from llm_d_bench.utils.kubernetes_commands import sdk_enabled

    if sdk_enabled():
        return scoped_runner(cluster_id)
    if cluster_id is None:
        return runner
    return ScopedCommandRunner(kubeconfig_environment(cluster_id))


async def installer_path(cluster_id: str | None = None) -> Path | None:
    root = await repo_root(cluster_id)
    if root is None:
        return None
    path = (root / "guides/recipes/observability/install-prometheus-grafana.sh").resolve()
    if not path.is_relative_to(root):
        return None
    return path


async def dashboards_dir(cluster_id: str | None = None) -> Path | None:
    root = await repo_root(cluster_id)
    if root is None:
        return None
    path = (root / "guides/recipes/observability/grafana/dashboards").resolve()
    if not path.is_relative_to(root):
        return None
    return path


async def expected_dashboard_names(cluster_id: str | None = None) -> list[str]:
    path = await dashboards_dir(cluster_id)
    if path is None or not path.is_dir():
        return []
    return sorted(entry.stem for entry in path.glob("*.json"))


async def get_status(namespace: str, *, cluster_id: str | None = None) -> ClusterStackStatusResponse:
    namespace = validate_namespace(namespace)
    snapshot = await discover_cluster_stack(
        namespace, _runner(cluster_id), expected_dashboards=await expected_dashboard_names(cluster_id)
    )
    active = operations.active_id(snapshot.cluster.context, namespace)
    if active:
        snapshot.active_operation_id = active
        if snapshot.status not in {"unsupported", "unreachable"}:
            snapshot.status = "installing"
            snapshot.message = "Monitoring stack installation is in progress"
    return snapshot


async def verify_status(namespace: str, *, cluster_id: str | None = None) -> ClusterStackStatusResponse:
    """Read status without overlaying the operation currently being verified."""
    return await discover_cluster_stack(
        validate_namespace(namespace),
        _runner(cluster_id),
        expected_dashboards=await expected_dashboard_names(cluster_id),
    )


async def preflight(
    request: ClusterStackInstallRequest, *, cluster_id: str | None = None
) -> ClusterStackPreflightResponse:
    path = await installer_path(cluster_id)
    kubectl_available = runner.executable("kubectl") is not None
    helm_available = runner.executable("helm") is not None
    installer_available = path is not None and path.is_file() and os.access(path, os.X_OK)
    checks = [
        PreflightCheck(
            name="kubectl",
            passed=kubectl_available,
            message="kubectl is available" if kubectl_available else "kubectl is not installed",
        ),
        PreflightCheck(
            name="helm",
            passed=helm_available,
            message="helm is available" if helm_available else "helm is not installed",
        ),
        PreflightCheck(
            name="installer",
            passed=installer_available,
            message="llm-d monitoring installer is available"
            if installer_available
            else (
                "The cluster llm-d source is unavailable or lacks the monitoring installer; "
                "download its Software Versions"
            ),
        ),
    ]
    if not all(check.passed for check in checks[:2]):
        return ClusterStackPreflightResponse(allowed=False, status="unknown", checks=checks)
    try:
        snapshot = await get_status(request.namespace, cluster_id=cluster_id)
    except ClusterStackError as error:
        checks.append(PreflightCheck(name="cluster", passed=False, message=error.message))
        return ClusterStackPreflightResponse(allowed=False, status="unreachable", checks=checks)
    checks.append(
        PreflightCheck(
            name="cluster",
            passed=snapshot.cluster.reachable,
            message=snapshot.message if not snapshot.cluster.reachable else "Kubernetes cluster is reachable",
        )
    )
    reinstalling_existing = request.reinstall and snapshot.status in {"ready", "degraded"}
    status_allowed = snapshot.status == "absent" or reinstalling_existing
    checks.append(
        PreflightCheck(
            name="stack_state",
            passed=status_allowed,
            message=(
                "Monitoring stack can be installed"
                if snapshot.status == "absent"
                else "Existing monitoring stack will be reinstalled"
                if reinstalling_existing
                else f"Current stack status is {snapshot.status}"
            ),
        )
    )
    warnings = []
    if request.mode == "individual":
        warnings.append("Workload namespaces must be labeled for this individual monitoring stack")
    if reinstalling_existing:
        warnings.append(
            "Reinstalling uninstalls the existing release and deletes the monitoring namespace, "
            "permanently removing stored Prometheus metrics."
        )
    command: list[str] = []
    if path is not None:
        if reinstalling_existing:
            command = [
                str(path),
                "--namespace",
                request.namespace,
                "--uninstall",
                "&&",
                str(path),
                "--namespace",
                request.namespace,
            ]
        else:
            command = [str(path), "--namespace", request.namespace]
        if request.mode == "individual":
            command.append("--individual")
        if request.enable_tls:
            command.append("--enable-tls")
    return ClusterStackPreflightResponse(
        allowed=all(check.passed for check in checks),
        status=snapshot.status,
        checks=checks,
        warnings=warnings,
        command_preview=command,
    )


async def install(
    request: ClusterStackInstallRequest,
    *,
    idempotency_key: str | None,
    cluster_id: str | None = None,
) -> ClusterStackOperationResponse:
    result = await preflight(request, cluster_id=cluster_id)
    if not result.allowed:
        if result.status == "ready":
            raise ClusterStackError("ALREADY_INSTALLED", "Monitoring stack is already installed", status_code=409)
        if result.status == "degraded":
            raise ClusterStackError(
                "RESIDUAL_RESOURCES_FOUND",
                "Monitoring resources already exist and require manual repair",
                status_code=409,
            )
        raise ClusterStackError("PREFLIGHT_FAILED", "Monitoring installation preflight failed", status_code=400)
    snapshot = await get_status(request.namespace, cluster_id=cluster_id)
    script = await installer_path(cluster_id)
    if script is None:
        raise ClusterStackError("PREFLIGHT_FAILED", "llm-d monitoring installer is unavailable", status_code=400)
    try:
        return await operations.create(
            request,
            context=snapshot.cluster.context,
            script=script,
            verifier=lambda ns: verify_status(ns, cluster_id=cluster_id),
            idempotency_key=idempotency_key,
            cluster_id=cluster_id,
        )
    except RuntimeError as error:
        raise ClusterStackError(
            "INSTALLATION_IN_PROGRESS",
            f"Monitoring installation {error} is already in progress",
            retryable=True,
            status_code=409,
        ) from error


def get_operation(operation_id: str) -> ClusterStackOperationResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ClusterStackError("OPERATION_NOT_FOUND", "Monitoring operation was not found", status_code=404)
    operation = operations.get(operation_id)
    if operation is None:
        raise ClusterStackError("OPERATION_NOT_FOUND", "Monitoring operation was not found", status_code=404)
    return operation


_PROMETHEUS_PORT = 9090
_GRAFANA_PORT = 80








async def get_links(namespace: str, *, cluster_id: str | None = None) -> ClusterStackLinksResponse:
    """Discover Prometheus/Grafana in the Cluster infrastructure stack and open tunnels."""
    namespace = validate_namespace(namespace)
    scoped = _runner(cluster_id)
    result = await scoped.run(["kubectl", "get", "services", "-n", namespace, "-o", "json"])
    services = json.loads(result.stdout or "{}").get("items", []) if result.returncode == 0 else []

    targets = (
        ("prometheus", lambda name: name.endswith("-prometheus"), "Prometheus", _PROMETHEUS_PORT),
        ("grafana", lambda name: name.endswith("-grafana"), "Grafana", _GRAFANA_PORT),
    )
    links: list[ObservabilityLink] = []
    for kind, predicate, label, default_port in targets:
        item = _find_service(services, predicate)
        if item is None:
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=False,
                    message=f"No {label} service found in namespace {namespace}",
                )
            )
            continue
        service = _service_name(item)
        port = _service_port(item, default_port)
        try:
            local_port = (
                await ensure_port_forward(
                    namespace,
                    service,
                    port,
                    address="0.0.0.0",  # noqa: S104 - browser dashboard links target the Lens host
                    cluster_id=cluster_id,
                )
            ).local_port
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=True,
                    service=service,
                    namespace=namespace,
                    port=port,
                    local_port=local_port,
                )
            )
        except PortForwardError as error:
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=False,
                    service=service,
                    namespace=namespace,
                    port=port,
                    message=str(error),
                )
            )
    return ClusterStackLinksResponse(namespace=namespace, links=links)
