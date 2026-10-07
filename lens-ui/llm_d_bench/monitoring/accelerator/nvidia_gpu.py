"""NVIDIA GPU accelerator observability provider (DCGM exporter).

Discovery, access-mode detection, preflight and install-argument construction
for the standalone DCGM exporter Helm chart. Like the Intel xpumd provider it
reuses the existing Cluster infrastructure Prometheus/Grafana rather than
deploying its own monitoring stack (design decision D2/ND5), and it never
installs the host GPU driver or a GPU Operator (decision D1).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from llm_d_bench.hardware.providers.nvidia import hardware_present
from llm_d_bench.monitoring.cluster_stack.models import (
    ClusterStackStatusResponse,
    ComponentDiagnostic,
    HelmReleaseSummary,
)
from llm_d_bench.utils import kubernetes as k8s
from llm_d_bench.utils.shell import CommandNotFoundError, which

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

_CHART_REPO = "https://nvidia.github.io/dcgm-exporter/helm-charts"
_CHART_NAME = "dcgm-exporter"
_CHART_VERSION = "4.8.4"
_RELEASE_NAME = "dcgm-exporter"
_DEFAULT_NAMESPACE = "dcgm-exporter"
_ACCESS_MODES: list[GpuAccess] = ["plugin", "dra"]

_MONITORING_DEFAULT_NAMESPACE = "llm-d-monitoring"
_MONITORING_DEFAULT_RELEASE = "llmd"
# GPU Feature Discovery (nvdp 0.17) labels GPU nodes with the NFD PCI-vendor
# label; the legacy `nvidia.com/gpu.present` label it used to also set is gone,
# so the DCGM node selector and GPU-node detection must use the NFD label.
_NODE_LABEL = "feature.node.kubernetes.io/pci-10de.present"
_RELEASE_NOT_FOUND = ("release: not found", "release not found")
_LABEL = {"dra": "DRA driver", "plugin": "device plugin"}


def _runtime_class_name() -> str:
    """RuntimeClass the DCGM exporter must use to see NVML.

    Read from the NVIDIA hardware profile's ``driver.modes.plugin.options`` so
    the exporter matches the device plugin; running under the default runtime
    makes DCGM exit with ``ERROR_LIBRARY_NOT_FOUND``.
    """
    try:
        from llm_d_bench.hardware.registry import get_profile

        profile = get_profile("nvidia")
    except Exception:  # pragma: no cover - discovery must not break install-arg construction
        profile = None
    driver = profile.driver if profile is not None else None
    mode = driver.mode("plugin") if driver is not None else None
    value = (mode.options or {}).get("runtime_class_name") if mode is not None else None
    return str(value) if value else "nvidia"


def _json(result: Any, default: Any) -> Any:
    if result.returncode != 0 or not result.stdout.strip():
        return default
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AcceleratorError(
            "ACCELERATOR_QUERY_FAILED",
            "Unable to parse helm response",
            retryable=True,
            status_code=502,
        ) from error


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


def _exporter_component(resources: list[dict[str, Any]]) -> AcceleratorComponent:
    daemonsets = [item for item in resources if item.get("kind") == "DaemonSet"]
    if not daemonsets:
        return AcceleratorComponent(name="dcgm_exporter", status="missing", source="none")
    ready = sum(int((item.get("status") or {}).get("numberReady", 0) or 0) for item in daemonsets)
    desired = sum(int((item.get("status") or {}).get("desiredNumberScheduled", 0) or 0) for item in daemonsets)
    diagnostics = [
        ComponentDiagnostic(
            severity="warning",
            code="WORKLOAD_NOT_READY",
            message=f"{int((item.get('status') or {}).get('numberReady', 0) or 0)}/"
            f"{int((item.get('status') or {}).get('desiredNumberScheduled', 0) or 0)} replicas are ready",
            resource=f"daemonset/{_metadata(item).get('name')}",
        )
        for item in daemonsets
        if not _daemonset_ready(item)
    ]
    status = "ready" if desired > 0 and ready >= desired else "progressing"
    return AcceleratorComponent(
        name="dcgm_exporter", status=status, ready=ready, desired=desired, source="dcgm_exporter",
        diagnostics=diagnostics,
    )


def _crd_component(name: str, items: list[dict[str, Any]], source: str = "dcgm_exporter") -> AcceleratorComponent:
    if not items:
        return AcceleratorComponent(name=name, status="missing", kind="crd", source="none")
    return AcceleratorComponent(
        name=name, status="ready", kind="crd", ready=len(items), desired=len(items), source=source
    )


def _dashboard_component(items: list[dict[str, Any]]) -> AcceleratorComponent:
    if not items:
        return AcceleratorComponent(name="grafana_dashboard", status="missing", kind="configmap", source="none")
    return AcceleratorComponent(
        name="grafana_dashboard", status="ready", kind="configmap",
        ready=len(items), desired=len(items), source="dcgm_exporter",
    )


def _gpu_nodes_component(nodes: list[dict[str, Any]]) -> AcceleratorComponent:
    count = len(nodes)
    if count == 0:
        return AcceleratorComponent(
            name="gpu_nodes", status="missing", kind="node", ready=0, desired=0, source="none",
            diagnostics=[
                ComponentDiagnostic(
                    severity="warning",
                    code="NO_GPU_NODES",
                    message=f"No nodes are labeled with {_NODE_LABEL}=true",
                )
            ],
        )
    return AcceleratorComponent(
        name="gpu_nodes", status="ready", kind="node", ready=count, desired=count, source="cluster"
    )


def _availability(mode: GpuAccess, items: list[dict[str, Any]]) -> AccessModeAvailability:
    label = _LABEL.get(mode, "driver")
    if not items:
        return AccessModeAvailability(
            mode=mode, available=False, detected=False,
            message=f"NVIDIA GPU {label} DaemonSet was not detected; deploy it first",
        )
    if any(_daemonset_ready(item) for item in items):
        return AccessModeAvailability(mode=mode, available=True, detected=True)
    return AccessModeAvailability(mode=mode, available=False, detected=True, message=f"NVIDIA GPU {label} is not ready")


def _derive_status(
    release: HelmReleaseSummary,
    exporter: AcceleratorComponent,
    sm_component: AcceleratorComponent,
    dashboard_component: AcceleratorComponent,
    release_missing: bool,
    active_operation_id: str | None,
) -> tuple[str, str]:
    if release_missing and exporter.status == "missing":
        return "absent", "NVIDIA GPU observability is not installed"
    if active_operation_id or exporter.status == "progressing":
        return "installing", "NVIDIA GPU observability is becoming ready"
    # The DCGM exporter chart ships a ServiceMonitor but no Grafana dashboard, so
    # a missing dashboard is "not enabled" (optional components are satisfied
    # when absent) and must not keep a healthy install degraded.
    dashboard_ok = dashboard_component.status in {"ready", "missing"}
    if (
        release.status == "deployed"
        and exporter.status == "ready"
        and sm_component.status == "ready"
        and dashboard_ok
    ):
        return "ready", "NVIDIA GPU observability is ready"
    return "degraded", "NVIDIA GPU observability requires attention"


class NvidiaGpuProvider:
    def capability(self) -> AcceleratorCapability:
        return AcceleratorCapability(
            type="nvidia_gpu",
            display_name="NVIDIA GPU",
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
            context = await k8s.current_context(cluster_id)
            reachable = await k8s.cluster_info(cluster_id)
        except CommandNotFoundError as error:
            raise AcceleratorError("PREFLIGHT_FAILED", str(error), status_code=400) from error
        except TimeoutError as error:
            raise AcceleratorError(
                "COMMAND_TIMEOUT", "Timed out while querying Kubernetes", retryable=True, status_code=504
            ) from error

        if reachable.returncode != 0:
            return AcceleratorStatusResponse(
                accelerator="nvidia_gpu",
                cluster_reachable=False,
                context=context,
                namespace=namespace,
                release=HelmReleaseSummary(name=_RELEASE_NAME),
                status="unreachable",
                message=reachable.stderr.strip() or "Kubernetes cluster is unreachable",
                observed_at=observed_at,
                active_operation_id=active_operation_id,
            )

        helm = await k8s.helm_status(_RELEASE_NAME, namespace, cluster_id=cluster_id)
        helm_payload = _json(helm, {})
        helm_error = f"{helm.stdout}\n{helm.stderr}".lower()
        release_missing = helm.returncode != 0 and any(marker in helm_error for marker in _RELEASE_NOT_FOUND)
        chart = None
        if helm.returncode == 0:
            listed = await k8s.helm_list(namespace, filter_regex=f"^{_RELEASE_NAME}$", cluster_id=cluster_id)
            if listed:
                chart = listed[0].get("chart")
        release = HelmReleaseSummary(
            name=_RELEASE_NAME,
            status=((helm_payload.get("info") or {}).get("status") if helm_payload else None),
            chart=chart,
            revision=helm_payload.get("version"),
        )

        resources = await k8s.list_resources(
            "daemonsets,deployments", namespace=namespace, selector=f"app.kubernetes.io/name={_RELEASE_NAME}",
            cluster_id=cluster_id,
        )
        exporter = _exporter_component(resources)

        servicemonitors = await k8s.list_resources("servicemonitors", namespace=namespace, cluster_id=cluster_id)
        exporter_sms = [
            item
            for item in servicemonitors
            if _RELEASE_NAME in _name(item) or _labels(item).get("app.kubernetes.io/name") == _RELEASE_NAME
        ]
        sm_component = _crd_component("prometheus_service_monitor", exporter_sms)

        dashboards = await k8s.list_resources(
            "configmaps", namespace=monitoring_namespace, selector="grafana_dashboard=1", cluster_id=cluster_id
        )
        dcgm_dashboards = [item for item in dashboards if "dcgm" in _name(item) or "nvidia" in _name(item)]
        dashboard_component = _dashboard_component(dcgm_dashboards)

        nodes = await k8s.list_resources("nodes", selector=f"{_NODE_LABEL}=true", cluster_id=cluster_id)
        gpu_nodes_component = _gpu_nodes_component(nodes)

        daemonsets = await k8s.list_resources("daemonsets", all_namespaces=True, cluster_id=cluster_id)
        plugin_ds = [item for item in daemonsets if "nvidia-device-plugin" in _name(item)]
        dra_ds = [item for item in daemonsets if "nvidia-dra-driver" in _name(item)]
        access_modes = [_availability("plugin", plugin_ds), _availability("dra", dra_ds)]

        inferred_mode = None
        if helm.returncode == 0:
            values = await k8s.helm_get_values(_RELEASE_NAME, namespace, cluster_id=cluster_id)
            gpu_access = (values or {}).get("gpuAccess")
            if gpu_access in {"dra", "plugin"}:
                inferred_mode = gpu_access
        effective_access_mode = access_mode or inferred_mode

        components = [exporter, sm_component, dashboard_component, gpu_nodes_component]
        status, message = _derive_status(
            release, exporter, sm_component, dashboard_component, release_missing, active_operation_id
        )
        return AcceleratorStatusResponse(
            accelerator="nvidia_gpu",
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

    async def preflight(
        self,
        request: AcceleratorInstallRequest,
        cluster_id: str | None,
        *,
        monitoring_status: ClusterStackStatusResponse | None = None,
    ) -> AcceleratorPreflightResponse:
        checks = [
            PreflightCheck(
                name="kubectl",
                passed=which("kubectl") is not None,
                message="kubectl is available" if which("kubectl") else "kubectl is not installed",
            ),
            PreflightCheck(
                name="helm",
                passed=which("helm") is not None,
                message="helm is available" if which("helm") else "helm is not installed",
            ),
        ]
        if not all(check.passed for check in checks):
            return AcceleratorPreflightResponse(allowed=False, status="unknown", checks=checks)

        monitoring_namespace = (
            monitoring_status.namespace if monitoring_status else _MONITORING_DEFAULT_NAMESPACE
        )
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

        present = await hardware_present(cluster_id)
        checks.append(
            PreflightCheck(
                name="gpu_nodes",
                passed=present,
                blocking=True,
                message=(
                    "NVIDIA GPU hardware detected in the cluster"
                    if present
                    else "No NVIDIA GPU hardware detected (no node label, DRA ResourceSlice or nvidia.com/gpu "
                    "extended resource); installing would leave the workload pending"
                ),
            )
        )

        availability = {mode.mode: mode for mode in snapshot.access_modes}
        selected = availability.get(request.access_mode)
        mode_message = (
            "Access mode is not recognized"
            if selected is None
            else selected.message or f"{request.access_mode} access mode is available in this environment"
        )
        checks.append(
            PreflightCheck(
                name="access_mode_pods",
                passed=bool(selected and selected.available),
                message=mode_message,
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
                    else "Cluster infrastructure monitoring stack must be ready before installing DCGM exporter"
                ),
            )
        )

        status_allowed = snapshot.status in {"absent", "degraded", "ready"}
        checks.append(
            PreflightCheck(
                name="stack_state",
                passed=status_allowed,
                message=(
                    "NVIDIA GPU observability can be installed or reconciled"
                    if status_allowed
                    else f"Current NVIDIA GPU observability status is {snapshot.status}"
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

    async def prepare_nodes(self, cluster_id: str | None, *, access_mode: GpuAccess | None) -> None:
        """NVIDIA nodes are labeled by the device plugin / GPU Operator, not Lens."""
        del cluster_id, access_mode

    async def configure_monitoring(self, monitoring_namespace: str, cluster_id: str | None) -> None:
        """Provision the DCGM Grafana dashboard the chart does not ship."""
        from .dcgm_dashboard import dashboard_manifest

        result = await k8s.scoped_runner(cluster_id).run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(dashboard_manifest(monitoring_namespace)),
            timeout=30,
        )
        if result.returncode != 0:
            raise AcceleratorError(
                "DASHBOARD_CONFIG_FAILED",
                (result.stderr or result.stdout or "Unable to apply the DCGM Grafana dashboard").strip(),
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
        del monitoring_namespace  # ServiceMonitor is discovered by the shared Prometheus, namespace-agnostic
        argv = [
            "helm",
            "upgrade",
            "--install",
            _RELEASE_NAME,
            _CHART_NAME,
            "--repo",
            _CHART_REPO,
            "--version",
            _CHART_VERSION,
            "--set",
            "serviceMonitor.enabled=true",
            "--set",
            f"serviceMonitor.additionalLabels.release={monitoring_release}",
            "--set-string",
            r"nodeSelector.feature\.node\.kubernetes\.io/pci-10de\.present=true",
        ]
        argv += ["--set", f"runtimeClassName={_runtime_class_name()}"]
        argv += ["--namespace", request.namespace, "--create-namespace"]
        return argv


_NVIDIA_GPU_PROVIDER = NvidiaGpuProvider()
register(_NVIDIA_GPU_PROVIDER)
