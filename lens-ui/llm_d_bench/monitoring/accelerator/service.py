"""Use-case orchestration for accelerator observability management."""

from __future__ import annotations

from llm_d_bench.monitoring.service_links import (
    service_name as _service_name, service_port as _service_port, find_service as _find_service,
)

import base64
import json
import os
import re
from datetime import UTC, datetime

import httpx

from llm_d_bench.monitoring.cluster_stack import service as cluster_stack_service
from llm_d_bench.utils.kubernetes import (
    PortForwardError,
    create_namespace,
    ensure_port_forward,
    label_namespace,
    list_resources,
    namespace_exists,
)
from llm_d_bench.utils.kubernetes import validate_namespace as validate_namespace

from .errors import AcceleratorError
from .models import (
    AcceleratorCapabilitiesResponse,
    AcceleratorInstallRequest,
    AcceleratorLinksResponse,
    AcceleratorOperationResponse,
    AcceleratorPreflightResponse,
    AcceleratorStatusResponse,
    GpuAccess,
    HelmReleaseSummary,
    ObservabilityLink,
)
from .operations import AcceleratorOperationManager
from .registry import capabilities as registry_capabilities
from .registry import get_provider

_DEFAULT_MONITORING_NAMESPACE = "llm-d-monitoring"
_DEFAULT_MONITORING_RELEASE = "llmd"

operations = AcceleratorOperationManager()


def monitoring_namespace() -> str:
    return os.getenv("ACCELERATOR_MONITORING_NAMESPACE", _DEFAULT_MONITORING_NAMESPACE)


def monitoring_release() -> str:
    return os.getenv("ACCELERATOR_MONITORING_RELEASE", _DEFAULT_MONITORING_RELEASE)


async def _monitoring_snapshot(cluster_id: str | None = None):
    return await cluster_stack_service.get_status(monitoring_namespace(), cluster_id=cluster_id)


def capabilities() -> AcceleratorCapabilitiesResponse:
    return registry_capabilities()


def _no_cluster_status(accelerator: str, namespace: str) -> AcceleratorStatusResponse:
    return AcceleratorStatusResponse(
        accelerator=accelerator,
        cluster_reachable=False,
        namespace=namespace,
        release=HelmReleaseSummary(),
        status="unknown",
        message="No target cluster selected",
        observed_at=datetime.now(UTC),
        stale=True,
    )


async def get_status(
    accelerator: str,
    namespace: str,
    access_mode: GpuAccess | None = None,
    *,
    cluster_id: str | None = None,
) -> AcceleratorStatusResponse:
    provider = get_provider(accelerator)
    namespace = validate_namespace(namespace)
    if cluster_id is None:
        return _no_cluster_status(provider.capability().type, namespace)
    snapshot = await provider.discover_status(
        namespace,
        cluster_id,
        access_mode=access_mode,
        monitoring_namespace=monitoring_namespace(),
    )
    active = operations.active_id(snapshot.context, namespace)
    if active:
        snapshot.active_operation_id = active
        if snapshot.status not in {"unsupported", "unreachable"}:
            snapshot.status = "installing"
            snapshot.message = "Accelerator observability installation is in progress"
    return snapshot


async def verify_status(
    accelerator: str,
    namespace: str,
    access_mode: GpuAccess | None = None,
    *,
    cluster_id: str | None = None,
) -> AcceleratorStatusResponse:
    """Read status without overlaying the operation currently being verified."""
    provider = get_provider(accelerator)
    return await provider.discover_status(
        validate_namespace(namespace),
        cluster_id,
        access_mode=access_mode,
        monitoring_namespace=monitoring_namespace(),
    )


_PROMETHEUS_PORT = 9090
_GRAFANA_PORT = 80








def _slugify(title: str) -> str:
    """Approximate Grafana's URL slug for a dashboard title."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _decode_secret(raw: str | None) -> str | None:
    """Decode a Kubernetes Secret ``data`` value (base64 in JSON output)."""
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode()
    except (ValueError, UnicodeDecodeError):
        return raw


async def _grafana_admin_credentials(namespace: str, cluster_id: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(user, password)`` for the Grafana admin account, if discoverable."""
    items = await list_resources("secrets", namespace=namespace, cluster_id=cluster_id)
    for item in items:
        if not _service_name(item).endswith("-grafana"):
            continue
        data = item.get("data") or {}
        user = _decode_secret(data.get("admin-user")) or "admin"
        password = _decode_secret(data.get("admin-password"))
        if password:
            return user, password
    return None, None


async def _grafana_api_dashboard_path(
    local_port: int,
    namespace: str,
    cluster_id: str | None,
    title: str | None,
) -> str | None:
    """Resolve the xpumd dashboard url from the Grafana API over an open tunnel."""
    user, password = await _grafana_admin_credentials(namespace, cluster_id)
    if not password:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{local_port}", auth=(user, password), timeout=5.0
        ) as client:
            response = await client.get("/api/search", params={"type": "dash-db"})
            response.raise_for_status()
            dashboards = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    lowered_title = str(title).strip().lower() if title else ""
    for item in dashboards:
        if lowered_title and (item.get("title") or "").strip().lower() == lowered_title:
            return item.get("url")
    for item in dashboards:
        if lowered_title and lowered_title in (item.get("title") or "").lower():
            return item.get("url")
    for item in dashboards:
        item_title = (item.get("title") or "").lower()
        if "xpum" in item_title or "xpu" in item_title:
            return item.get("url")
    return None


async def _grafana_dashboard_path(
    namespace: str,
    cluster_id: str | None = None,
    *,
    local_port: int | None = None,
) -> str | None:
    """Resolve the xpumd Grafana dashboard deep-link.

    The preferred source is the uid Grafana actually serves, discovered through its
    API once a tunnel is open: the kube-prometheus-stack sidecar can rename a
    dashboard's uid when several ConfigMaps share the same ``dashboard.json`` key.
    The uid embedded in the xpumd dashboard ConfigMap is kept as a fallback.
    """
    items = await list_resources(
        "configmaps", namespace=namespace, selector="grafana_dashboard=1", cluster_id=cluster_id
    )
    candidates = [item for item in items if "xpumd" in _service_name(item) or "intel" in _service_name(item)]
    fallback_path: str | None = None
    title: str | None = None
    for item in candidates:
        for raw in (item.get("data") or {}).values():
            try:
                dashboard = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(dashboard, dict):
                continue
            uid = dashboard.get("uid")
            title = dashboard.get("title")
            if uid:
                slug = _slugify(str(title)) if title else ""
                fallback_path = f"/d/{uid}/{slug}" if slug else f"/d/{uid}"
            break
        if fallback_path:
            break

    if local_port is not None:
        # When a tunnel is open, the ConfigMap uid is not a reliable deep-link
        # target: the kube-prometheus-stack sidecar can rename the dashboard uid.
        # Prefer the uid Grafana actually serves (via its API); if that is not
        # resolvable, return no path so the frontend opens the Grafana root rather
        # than a broken "Dashboard not found" deep link.
        return await _grafana_api_dashboard_path(local_port, namespace, cluster_id, title)

    return fallback_path


async def get_links(accelerator: str, *, cluster_id: str | None = None) -> AcceleratorLinksResponse:
    """Discover Prometheus/Grafana in the Cluster infrastructure stack and open tunnels."""
    provider = get_provider(accelerator)  # validate the accelerator exists
    if cluster_id is None:
        return AcceleratorLinksResponse(accelerator=provider.capability().type, links=[])
    namespace = monitoring_namespace()
    services = await list_resources("services", namespace=namespace, cluster_id=cluster_id)

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
            dashboard_path = (
                await _grafana_dashboard_path(namespace, cluster_id, local_port=local_port)
                if kind == "grafana"
                else None
            )
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=True,
                    service=service,
                    namespace=namespace,
                    port=port,
                    local_port=local_port,
                    dashboard_path=dashboard_path,
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
    return AcceleratorLinksResponse(accelerator=accelerator, links=links)


async def preflight(
    request: AcceleratorInstallRequest, *, cluster_id: str | None = None
) -> AcceleratorPreflightResponse:
    provider = get_provider(request.accelerator)
    monitoring_snapshot = await _monitoring_snapshot(cluster_id)
    return await provider.preflight(request, cluster_id, monitoring_status=monitoring_snapshot)


async def _ensure_namespace(namespace: str, *, access_mode: GpuAccess | None, cluster_id: str | None = None) -> None:
    """Create the xpumd namespace and, for DRA, apply the admin-access label.

    ``resource.kubernetes.io/admin-access=true`` is required for DRA GPU monitoring
    and admin access; plugin access mode does not require it.
    """
    if not await namespace_exists(namespace, cluster_id=cluster_id):
        create = await create_namespace(namespace, cluster_id=cluster_id)
        if create.returncode != 0 and "already exists" not in (create.stdout + create.stderr).lower():
            raise AcceleratorError(
                "NAMESPACE_CREATE_FAILED",
                f"Unable to create namespace {namespace}: {create.stderr.strip()}",
                retryable=True,
                status_code=502,
            )
    if access_mode == "dra":
        label = await label_namespace(namespace, {"resource.kubernetes.io/admin-access": "true"}, cluster_id=cluster_id)
        if label.returncode != 0:
            raise AcceleratorError(
                "NAMESPACE_LABEL_FAILED",
                f"Unable to label namespace {namespace}: {label.stderr.strip()}",
                retryable=True,
                status_code=502,
            )


async def install(
    request: AcceleratorInstallRequest,
    *,
    idempotency_key: str | None,
    cluster_id: str | None = None,
) -> AcceleratorOperationResponse:
    provider = get_provider(request.accelerator)
    result = await preflight(request, cluster_id=cluster_id)
    if not result.allowed:
        raise AcceleratorError(
            "PREFLIGHT_FAILED", "Accelerator observability installation preflight failed", status_code=400
        )
    snapshot = await verify_status(request.accelerator, request.namespace, request.access_mode, cluster_id=cluster_id)
    monitoring_snapshot = await _monitoring_snapshot(cluster_id)
    namespace = monitoring_snapshot.namespace if monitoring_snapshot else monitoring_namespace()
    release = (
        monitoring_snapshot.release.name
        if monitoring_snapshot and monitoring_snapshot.release
        else monitoring_release()
    )
    await _ensure_namespace(request.namespace, access_mode=request.access_mode, cluster_id=cluster_id)
    await provider.prepare_nodes(cluster_id, access_mode=request.access_mode)
    argv = provider.install_argv(request, monitoring_namespace=namespace, monitoring_release=release)
    configure_monitoring = getattr(provider, "configure_monitoring", None)
    post_install = (lambda: configure_monitoring(namespace, cluster_id)) if callable(configure_monitoring) else None
    try:
        return await operations.create(
            request,
            context=snapshot.context,
            argv=argv,
            verifier=lambda ns: verify_status(request.accelerator, ns, request.access_mode, cluster_id=cluster_id),
            post_install=post_install,
            idempotency_key=idempotency_key,
            cluster_id=cluster_id,
        )
    except RuntimeError as error:
        raise AcceleratorError(
            "INSTALLATION_IN_PROGRESS",
            f"Accelerator observability installation {error} is already in progress",
            retryable=True,
            status_code=409,
        ) from error


def get_operation(operation_id: str) -> AcceleratorOperationResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise AcceleratorError(
            "OPERATION_NOT_FOUND", "Accelerator observability operation was not found", status_code=404
        )
    operation = operations.get(operation_id)
    if operation is None:
        raise AcceleratorError(
            "OPERATION_NOT_FOUND", "Accelerator observability operation was not found", status_code=404
        )
    return operation
