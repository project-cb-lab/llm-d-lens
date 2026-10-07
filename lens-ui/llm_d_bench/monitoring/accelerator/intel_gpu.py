"""Intel GPU (xpumd) accelerator provider.

Discovery, access-mode detection, preflight and install-argument construction for
the Intel XPUM Daemon Helm chart. Prometheus/Grafana are always enabled but rely on
the existing Cluster infrastructure stack (``prometheus.release`` /
``grafana.namespace``), never on a self-deployed monitoring stack.
"""

from __future__ import annotations

from llm_d_bench.monitoring.command_output import parse_command_json

import json
from datetime import UTC, datetime
from typing import Any

from llm_d_bench.monitoring.cluster_stack.models import (
    ClusterStackStatusResponse,
    ComponentDiagnostic,
    HelmReleaseSummary,
)
from llm_d_bench.utils.kubernetes import (
    cluster_info,
    current_context,
    get_namespace,
    helm_get_values,
    helm_list,
    helm_status,
    label_node,
    list_resources,
    scoped_runner,
)
from llm_d_bench.utils.shell import CommandNotFoundError, CommandResult, which

from .errors import AcceleratorError
from .models import (
    AcceleratorCapability,
    AcceleratorComponent,
    AcceleratorInstallRequest,
    AcceleratorPreflightResponse,
    AcceleratorStatusResponse,
    AccessModeAvailability,
    GpuAccess,
    PreflightCheck,
)
from .registry import register

_CHART_REPO = "oci://ghcr.io/intel/xpumanager/charts/xpumd"
_CHART_VERSION = "2.1.0"
_RELEASE_NAME = "xpumd"
_DEFAULT_NAMESPACE = "intel-xpumd"
_ACCESS_MODES: list[GpuAccess] = ["dra", "plugin"]

_MONITORING_DEFAULT_NAMESPACE = "llm-d-monitoring"
_MONITORING_DEFAULT_RELEASE = "llmd"

_RELEASE_NOT_FOUND = ("release: not found", "release not found")

_FALLBACK_GPU_NODE_LABEL_SELECTOR = {"intel.feature.node.kubernetes.io/gpu": "true"}


def _gpu_node_label_selector() -> dict[str, str]:
    """Intel GPU node label selector, from the hardware profile."""
    try:
        from llm_d_bench.hardware.registry import get_profile

        selector = dict(get_profile("intel-xpu").node_label_selector)
    except Exception:  # pragma: no cover - discovery failure must not break preflight
        selector = {}
    return selector or dict(_FALLBACK_GPU_NODE_LABEL_SELECTOR)


def _gpu_node_selector_string() -> str:
    return ",".join(f"{key}={value}" for key, value in _gpu_node_label_selector().items())


def _profile_tuple(field: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    try:
        from llm_d_bench.hardware.registry import get_profile

        value = getattr(get_profile("intel-xpu"), field, None)
    except Exception:  # pragma: no cover - discovery failure must not break preflight
        value = None
    return tuple(value) if value else fallback


def _device_classes() -> tuple[str, ...]:
    return _profile_tuple("device_classes", ("gpu.intel.com",))


def _resource_prefixes() -> tuple[str, ...]:
    return _profile_tuple("resource_prefixes", ("gpu.intel.com/",))


def _monitor_suffixes() -> tuple[str, ...]:
    return _profile_tuple("monitor_resource_suffixes", ("monitoring",))


async def _intel_hardware_present(cluster_id: str | None) -> bool:
    """Read-only presence probe: node label, DRA ResourceSlice or extended resource.

    Presence must not mutate the cluster (preflight is read-only), so the
    ``presence.ensure_refs`` NFD apply from the profile is deliberately not used
    here -- only signals that already exist are inspected.
    """
    if await list_resources("nodes", selector=_gpu_node_selector_string(), cluster_id=cluster_id):
        return True
    try:
        slices = await list_resources("resourceslices", cluster_id=cluster_id)
    except Exception:
        slices = []
    device_classes = _device_classes()
    if any(str((item.get("spec") or {}).get("driver") or "") in device_classes for item in slices):
        return True
    prefixes = _resource_prefixes()
    suffixes = _monitor_suffixes()
    for node in await list_resources("nodes", cluster_id=cluster_id):
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        for key in allocatable:
            if any(str(key).startswith(prefix) for prefix in prefixes) and not any(
                str(key).endswith(f"/{suffix}") for suffix in suffixes
            ):
                return True
    return False


def _compute_engine_dashboard(raw: str) -> tuple[str, bool]:
    """Restrict GPU overview panels to the aggregate compute engine metric."""
    try:
        dashboard = json.loads(raw)
    except json.JSONDecodeError:
        return raw, False
    changed = False

    def visit(panels: list[dict[str, Any]]) -> None:
        nonlocal changed
        for panel in panels:
            if panel.get("title") == "GPU Utilization":
                for target in panel.get("targets") or []:
                    expression = target.get("expr")
                    if not isinstance(expression, str) or "hw_gpu_utilization_ratio" not in expression:
                        continue
                    if 'hw_gpu_task="compute-all"' in expression:
                        continue
                    target["expr"] = expression.replace(
                        "hw_gpu_utilization_ratio{",
                        'hw_gpu_utilization_ratio{hw_gpu_task="compute-all",',
                    )
                    changed = changed or target["expr"] != expression
            visit(panel.get("panels") or [])

    visit(dashboard.get("panels") or [])
    return (json.dumps(dashboard, separators=(",", ":")), True) if changed else (raw, False)


def _json(result: CommandResult, default: Any) -> Any:
    return parse_command_json(result, default, error_factory=AcceleratorError, code="ACCELERATOR_QUERY_FAILED")


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("metadata") or {}


def _labels(item: dict[str, Any]) -> dict[str, Any]:
    return _metadata(item).get("labels") or {}


def _name(item: dict[str, Any]) -> str:
    return str(_metadata(item).get("name") or "").lower()


def _daemonset_ready(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    desired = int(status.get("desiredNumberScheduled", 0) or 0)
    ready = int(status.get("numberReady", 0) or 0)
    return desired > 0 and ready >= desired


def _is_dra_driver(item: dict[str, Any]) -> bool:
    name = _name(item)
    namespace = str(_metadata(item).get("namespace") or "")
    labels = _labels(item)
    return (
        "intel-gpu-resource-driver" in name
        or namespace == "intel-gpu-resource-driver"
        or labels.get("app.kubernetes.io/name") == "intel-gpu-resource-driver"
    )


def _is_gpu_plugin(item: dict[str, Any]) -> bool:
    name = _name(item)
    labels = _labels(item)
    return "intel-gpu-plugin" in name or labels.get("app") == "intel-gpu-plugin"


def _xpumd_component(resources: list[dict[str, Any]]) -> AcceleratorComponent:
    daemonsets = [item for item in resources if item.get("kind") == "DaemonSet"]
    if not daemonsets:
        return AcceleratorComponent(name="xpumd", status="missing", source="none")
    ready_values: list[int] = []
    desired_values: list[int] = []
    diagnostics: list[ComponentDiagnostic] = []
    for item in daemonsets:
        status = item.get("status") or {}
        desired = int(status.get("desiredNumberScheduled", 0) or 0)
        ready = int(status.get("numberReady", 0) or 0)
        desired_values.append(desired)
        ready_values.append(ready)
        if ready < desired:
            diagnostics.append(
                ComponentDiagnostic(
                    severity="warning",
                    code="WORKLOAD_NOT_READY",
                    message=f"{ready}/{desired} replicas are ready",
                    resource=f"daemonset/{_metadata(item).get('name')}",
                )
            )
    desired = sum(desired_values)
    ready = sum(ready_values)
    status = "ready" if desired > 0 and ready >= desired else "progressing"
    return AcceleratorComponent(
        name="xpumd",
        status=status,
        ready=ready,
        desired=desired,
        source="xpumd",
        diagnostics=diagnostics,
    )


def _servicemonitor_component(items: list[dict[str, Any]]) -> AcceleratorComponent:
    if not items:
        return AcceleratorComponent(name="prometheus_service_monitor", status="missing", kind="crd", source="none")
    return AcceleratorComponent(
        name="prometheus_service_monitor",
        status="ready",
        kind="crd",
        ready=len(items),
        desired=len(items),
        source="xpumd",
    )


def _dashboard_component(items: list[dict[str, Any]]) -> AcceleratorComponent:
    if not items:
        return AcceleratorComponent(name="grafana_dashboard", status="missing", kind="configmap", source="none")
    return AcceleratorComponent(
        name="grafana_dashboard",
        status="ready",
        kind="configmap",
        ready=len(items),
        desired=len(items),
        source="xpumd",
    )


def _gpu_nodes_component(nodes: list[dict[str, Any]]) -> AcceleratorComponent:
    count = len(nodes)
    if count == 0:
        return AcceleratorComponent(
            name="gpu_nodes",
            status="missing",
            kind="node",
            ready=0,
            desired=0,
            source="none",
            diagnostics=[
                ComponentDiagnostic(
                    severity="warning",
                    code="NO_GPU_NODES",
                    message="No nodes are labeled with intel.feature.node.kubernetes.io/gpu=true",
                )
            ],
        )
    return AcceleratorComponent(
        name="gpu_nodes", status="ready", kind="node", ready=count, desired=count, source="cluster"
    )


def _plugin_monitoring_enabled(items: list[dict[str, Any]]) -> bool:
    """Return True when a plugin DaemonSet advertises ``gpu.intel.com/monitoring``.

    The Intel GPU plugin only registers the ``gpu.intel.com/monitoring`` extended
    resource when started with the ``-enable-monitoring`` flag; without it the
    xpumd DaemonSet stays Pending with ``Insufficient gpu.intel.com/monitoring``.
    """
    for item in items:
        pod_spec = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
        for container in pod_spec.get("containers") or []:
            if "-enable-monitoring" in (container.get("args") or []):
                return True
    return False


def _availability(mode: GpuAccess, items: list[dict[str, Any]]) -> AccessModeAvailability:
    if not items:
        missing = (
            "DRA driver DaemonSet was not detected; deploy the Intel GPU DRA driver first"
            if mode == "dra"
            else "GPU device plugin DaemonSet was not detected; deploy intel-gpu-plugin first"
        )
        return AccessModeAvailability(mode=mode, available=False, detected=False, message=missing)
    if any(_daemonset_ready(item) for item in items):
        if mode == "plugin" and not _plugin_monitoring_enabled(items):
            return AccessModeAvailability(
                mode=mode,
                available=False,
                detected=True,
                message=(
                    "intel-gpu-plugin is running but monitoring access is disabled; "
                    "add -enable-monitoring to the plugin DaemonSet arguments"
                ),
            )
        return AccessModeAvailability(mode=mode, available=True, detected=True)
    not_ready = (
        "DRA driver DaemonSet was detected but is not ready"
        if mode == "dra"
        else "intel-gpu-plugin DaemonSet was detected but is not ready"
    )
    return AccessModeAvailability(mode=mode, available=False, detected=True, message=not_ready)


def _resource_slice_node_names(items: list[dict[str, Any]]) -> set[str]:
    """Return node names that publish Intel GPU DRA ResourceSlices."""
    names: set[str] = set()
    for item in items:
        spec = item.get("spec") or {}
        if spec.get("driver") != "gpu.intel.com":
            continue
        node_name = spec.get("nodeName")
        if node_name:
            names.add(str(node_name))
    return names


def _extended_resource_node_names(nodes: list[dict[str, Any]]) -> set[str]:
    """Return node names that advertise ``gpu.intel.com/*`` extended resources."""
    names: set[str] = set()
    for node in nodes:
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        if any(str(key).startswith("gpu.intel.com/") for key in allocatable):
            name = _metadata(node).get("name")
            if name:
                names.add(str(name))
    return names


async def _namespace_has_admin_access_label(namespace: str, cluster_id: str | None) -> bool:
    namespace_obj = await get_namespace(namespace, cluster_id=cluster_id)
    if namespace_obj is None:
        return False
    labels = (namespace_obj.get("metadata") or {}).get("labels") or {}
    return labels.get("resource.kubernetes.io/admin-access") == "true"


class IntelGpuProvider:
    def capability(self) -> AcceleratorCapability:
        return AcceleratorCapability(
            type="intel_gpu",
            display_name="Intel GPU",
            access_modes=list(_ACCESS_MODES),
            release_name=_RELEASE_NAME,
            default_namespace=_DEFAULT_NAMESPACE,
        )

    async def discover_status(
        self,
        namespace: str,
        cluster_id: str | None,
        *,
        access_mode: GpuAccess | None = None,
        monitoring_namespace: str | None = None,
        active_operation_id: str | None = None,
    ) -> AcceleratorStatusResponse:
        observed_at = datetime.now(UTC)
        monitoring_namespace = monitoring_namespace or _MONITORING_DEFAULT_NAMESPACE
        try:
            context = await current_context(cluster_id)
            reachable = await cluster_info(cluster_id)
        except CommandNotFoundError as error:
            raise AcceleratorError("PREFLIGHT_FAILED", str(error), status_code=400) from error
        except TimeoutError as error:
            raise AcceleratorError(
                "COMMAND_TIMEOUT", "Timed out while querying Kubernetes", retryable=True, status_code=504
            ) from error

        if reachable.returncode != 0:
            return AcceleratorStatusResponse(
                accelerator="intel_gpu",
                cluster_reachable=False,
                context=context,
                namespace=namespace,
                release=HelmReleaseSummary(name=_RELEASE_NAME),
                status="unreachable",
                message=reachable.stderr.strip() or "Kubernetes cluster is unreachable",
                observed_at=observed_at,
                active_operation_id=active_operation_id,
            )

        helm = await helm_status(_RELEASE_NAME, namespace, cluster_id=cluster_id)
        helm_payload = _json(helm, {})
        helm_error = f"{helm.stdout}\n{helm.stderr}".lower()
        release_missing = helm.returncode != 0 and any(marker in helm_error for marker in _RELEASE_NOT_FOUND)
        chart = None
        if helm.returncode == 0:
            listed = await helm_list(namespace, filter_regex=f"^{_RELEASE_NAME}$", cluster_id=cluster_id)
            if listed:
                chart = listed[0].get("chart")
        release = HelmReleaseSummary(
            name=_RELEASE_NAME,
            status=((helm_payload.get("info") or {}).get("status") if helm_payload else None),
            chart=chart,
            revision=helm_payload.get("version"),
        )

        resources = await list_resources(
            "daemonsets,services",
            namespace=namespace,
            selector=f"app.kubernetes.io/name={_RELEASE_NAME}",
            cluster_id=cluster_id,
        )
        xpumd_component = _xpumd_component(resources)

        servicemonitors = await list_resources("servicemonitors", namespace=namespace, cluster_id=cluster_id)
        xpumd_sms = [
            item
            for item in servicemonitors
            if _RELEASE_NAME in _name(item) or _labels(item).get("app.kubernetes.io/name") == _RELEASE_NAME
        ]
        sm_component = _servicemonitor_component(xpumd_sms)

        dashboards = await list_resources(
            "configmaps",
            namespace=monitoring_namespace,
            selector="grafana_dashboard=1",
            cluster_id=cluster_id,
        )
        xpumd_dashboards = [item for item in dashboards if _RELEASE_NAME in _name(item) or "intel" in _name(item)]
        dashboard_component = _dashboard_component(xpumd_dashboards)

        nodes = await list_resources(
            "nodes",
            selector=_gpu_node_selector_string(),
            cluster_id=cluster_id,
        )
        gpu_nodes_component = _gpu_nodes_component(nodes)

        daemonsets = await list_resources("daemonsets", all_namespaces=True, cluster_id=cluster_id)
        dra_ds = [item for item in daemonsets if _is_dra_driver(item)]
        plugin_ds = [item for item in daemonsets if _is_gpu_plugin(item)]
        access_modes = [_availability("dra", dra_ds), _availability("plugin", plugin_ds)]

        inferred_mode = None
        if helm.returncode == 0:
            values = await helm_get_values(_RELEASE_NAME, namespace, cluster_id=cluster_id)
            gpu_access = (values or {}).get("gpuAccess")
            if gpu_access in {"dra", "plugin"}:
                inferred_mode = gpu_access
        effective_access_mode = access_mode or inferred_mode

        components = [xpumd_component, sm_component, dashboard_component, gpu_nodes_component]
        status, message = _derive_status(
            release, xpumd_component, sm_component, dashboard_component, release_missing, active_operation_id
        )

        return AcceleratorStatusResponse(
            accelerator="intel_gpu",
            access_mode=effective_access_mode,
            access_modes=access_modes,
            cluster_reachable=True,
            context=context,
            namespace=namespace,
            release=release,
            status=status,
            message=message,
            components=components,
            active_operation_id=active_operation_id,
            observed_at=observed_at,
        )

    async def configure_monitoring(self, monitoring_namespace: str, cluster_id: str | None) -> None:
        """Make xpumd's Grafana GPU overview report compute-engine utilization."""
        dashboards = await list_resources(
            "configmaps",
            namespace=monitoring_namespace,
            selector="grafana_dashboard=1",
            cluster_id=cluster_id,
        )
        runner = scoped_runner(cluster_id)
        for dashboard in dashboards:
            name = str(_metadata(dashboard).get("name") or "")
            if _RELEASE_NAME not in name.lower() and "intel" not in name.lower():
                continue
            raw = (dashboard.get("data") or {}).get("dashboard.json")
            if not isinstance(raw, str):
                continue
            normalized, changed = _compute_engine_dashboard(raw)
            if not changed:
                continue
            result = await runner.run(
                [
                    "kubectl",
                    "patch",
                    "configmap",
                    name,
                    "--namespace",
                    monitoring_namespace,
                    "--type=merge",
                    "--patch",
                    json.dumps({"data": {"dashboard.json": normalized}}),
                ],
                timeout=30,
            )
            if not result.ok:
                raise AcceleratorError(
                    "DASHBOARD_CONFIG_FAILED",
                    (result.stderr or result.stdout or "Unable to configure xpumd Grafana dashboard").strip(),
                    retryable=True,
                    status_code=502,
                )

    async def preflight(
        self,
        request: AcceleratorInstallRequest,
        cluster_id: str | None,
        *,
        monitoring_status: ClusterStackStatusResponse | None = None,
    ) -> AcceleratorPreflightResponse:
        kubectl_available = which("kubectl") is not None
        helm_available = which("helm") is not None
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
        ]
        if not all(check.passed for check in checks):
            return AcceleratorPreflightResponse(allowed=False, status="unknown", checks=checks)

        monitoring_namespace = monitoring_status.namespace if monitoring_status else _MONITORING_DEFAULT_NAMESPACE
        try:
            snapshot = await self.discover_status(
                request.namespace,
                cluster_id,
                access_mode=request.access_mode,
                monitoring_namespace=monitoring_namespace,
            )
        except AcceleratorError as error:
            checks.append(PreflightCheck(name="cluster", passed=False, message=error.message))
            return AcceleratorPreflightResponse(allowed=False, status="unreachable", checks=checks)

        checks.append(
            PreflightCheck(
                name="cluster",
                passed=snapshot.cluster_reachable,
                message=(snapshot.message if not snapshot.cluster_reachable else "Kubernetes cluster is reachable"),
            )
        )
        if not snapshot.cluster_reachable:
            return AcceleratorPreflightResponse(
                allowed=False, status=snapshot.status, checks=checks, warnings=[snapshot.message]
            )

        availability = {mode.mode: mode for mode in snapshot.access_modes}
        selected = availability.get(request.access_mode)
        if selected is None:
            mode_message = "Access mode is not recognized"
        elif selected.available:
            mode_message = f"{request.access_mode} access mode is available in this environment"
        else:
            mode_message = selected.message or f"{request.access_mode} access mode is not available"
        checks.append(
            PreflightCheck(
                name="access_mode_pods",
                passed=bool(selected and selected.available),
                message=mode_message,
            )
        )

        if request.access_mode == "dra":
            access_labeled = await _namespace_has_admin_access_label(request.namespace, cluster_id)
            checks.append(
                PreflightCheck(
                    name="access_label",
                    passed=access_labeled,
                    blocking=False,
                    message=(
                        "Namespace is labeled resource.kubernetes.io/admin-access=true"
                        if access_labeled
                        else "Namespace admin-access label is missing; it will be applied during install"
                    ),
                )
            )

        hardware_present = await _intel_hardware_present(cluster_id)
        checks.append(
            PreflightCheck(
                name="gpu_nodes",
                passed=hardware_present,
                blocking=True,
                message=(
                    "Intel GPU hardware detected in the cluster"
                    if hardware_present
                    else (
                        "No Intel GPU hardware detected (no node label, DRA ResourceSlice or extended "
                        "resource); installing would leave the workload pending"
                    )
                ),
            )
        )

        monitoring_ready = monitoring_status is not None and monitoring_status.status == "ready"
        checks.append(
            PreflightCheck(
                name="monitoring_stack",
                passed=monitoring_ready,
                message=(
                    "Cluster infrastructure monitoring stack is ready"
                    if monitoring_ready
                    else "Cluster infrastructure monitoring stack must be ready before installing xpumd"
                ),
            )
        )

        status_allowed = snapshot.status in {"absent", "degraded", "ready"}
        checks.append(
            PreflightCheck(
                name="stack_state",
                passed=status_allowed,
                message=(
                    "Intel GPU observability can be installed or reconciled"
                    if status_allowed
                    else f"Current Intel GPU observability status is {snapshot.status}"
                ),
            )
        )

        monitoring_release = (
            monitoring_status.release.name
            if monitoring_status and monitoring_status.release
            else _MONITORING_DEFAULT_RELEASE
        )
        command = self.install_argv(
            request, monitoring_namespace=monitoring_namespace, monitoring_release=monitoring_release
        )

        return AcceleratorPreflightResponse(
            allowed=all(check.passed for check in checks if check.blocking),
            status=snapshot.status,
            checks=checks,
            warnings=[],
            command_preview=command,
        )

    async def prepare_nodes(
        self,
        cluster_id: str | None,
        *,
        access_mode: GpuAccess | None,
    ) -> None:
        """Label GPU nodes with ``intel.feature.node.kubernetes.io/gpu=true``.

        GPU nodes are detected per access mode (DRA via ResourceSlices, plugin via
        ``gpu.intel.com/*`` extended resources) so the xpumd DaemonSet nodeSelector
        matches without requiring NFD to have pre-labeled the nodes.
        """
        if access_mode == "dra":
            items = await list_resources("resourceslices", cluster_id=cluster_id)
            node_names = _resource_slice_node_names(items)
        else:
            items = await list_resources("nodes", cluster_id=cluster_id)
            node_names = _extended_resource_node_names(items)
        for node_name in sorted(node_names):
            label = await label_node(
                node_name,
                _gpu_node_label_selector(),
                cluster_id=cluster_id,
            )
            if label.returncode != 0:
                raise AcceleratorError(
                    "NODE_LABEL_FAILED",
                    f"Unable to label GPU node {node_name}: {label.stderr.strip()}",
                    retryable=True,
                    status_code=502,
                )

    def install_argv(
        self,
        request: AcceleratorInstallRequest,
        *,
        monitoring_namespace: str,
        monitoring_release: str,
    ) -> list[str]:
        return [
            "helm",
            "upgrade",
            "--install",
            _RELEASE_NAME,
            _CHART_REPO,
            "--version",
            _CHART_VERSION,
            "--set",
            f"gpuAccess={request.access_mode}",
            "--set-string",
            r"nodeSelector.intel\.feature\.node\.kubernetes\.io/gpu=true",
            "--set",
            "prometheus.monitor=true",
            "--set",
            f"prometheus.release={monitoring_release}",
            "--set",
            "grafana.dashboards=true",
            "--set",
            f"grafana.namespace={monitoring_namespace}",
            "--set",
            "config.service.pipelines.metrics.exporters={intel_xpu_info,prometheus}",
            "--namespace",
            request.namespace,
        ]


def _derive_status(
    release: HelmReleaseSummary,
    xpumd_component: AcceleratorComponent,
    sm_component: AcceleratorComponent,
    dashboard_component: AcceleratorComponent,
    release_missing: bool,
    active_operation_id: str | None,
) -> tuple[str, str]:
    if release_missing and xpumd_component.status == "missing":
        return "absent", "Intel GPU observability is not installed"
    if active_operation_id or xpumd_component.status == "progressing":
        return "installing", "Intel GPU observability is becoming ready"
    if (
        release.status == "deployed"
        and xpumd_component.status == "ready"
        and sm_component.status == "ready"
        and dashboard_component.status == "ready"
    ):
        return "ready", "Intel GPU observability is ready"
    return "degraded", "Intel GPU observability requires attention"


_INTEL_GPU_PROVIDER = IntelGpuProvider()
register(_INTEL_GPU_PROVIDER)
