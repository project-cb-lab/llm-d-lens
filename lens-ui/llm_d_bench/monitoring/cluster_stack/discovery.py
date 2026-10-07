"""Kubernetes discovery and health aggregation for the monitoring stack."""

from __future__ import annotations

from llm_d_bench.monitoring.command_output import parse_command_json

import json
from datetime import UTC, datetime
from typing import Any

from llm_d_bench.utils.shell import CommandNotFoundError, CommandResult, CommandRunner

from .errors import ClusterStackError
from .models import (
    ClusterStackComponent,
    ClusterStackStatusResponse,
    ClusterSummary,
    ComponentDiagnostic,
    HelmReleaseSummary,
)

_RELEASE_NOT_FOUND = ("release: not found", "release not found")


def _json(result: CommandResult, default: Any) -> Any:
    return parse_command_json(result, default, error_factory=ClusterStackError, code="CLUSTER_QUERY_FAILED")


def _resource_ready(item: dict[str, Any]) -> tuple[int | None, int | None, list[ComponentDiagnostic]]:
    kind = str(item.get("kind") or "")
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    name = str(metadata.get("name") or "")
    diagnostics: list[ComponentDiagnostic] = []
    if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        desired_key = "desiredNumberScheduled" if kind == "DaemonSet" else "replicas"
        ready_key = "numberReady" if kind == "DaemonSet" else "readyReplicas"
        desired = int(status.get(desired_key, spec.get("replicas", 1)) or 0)
        ready = int(status.get(ready_key, 0) or 0)
        if ready < desired:
            diagnostics.append(
                ComponentDiagnostic(
                    severity="warning",
                    code="WORKLOAD_NOT_READY",
                    message=f"{ready}/{desired} replicas are ready",
                    resource=f"{kind.lower()}/{name}",
                )
            )
        return ready, desired, diagnostics
    return None, None, diagnostics


def _pod_ready(item: dict[str, Any]) -> tuple[int, int, list[ComponentDiagnostic]]:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    name = str(metadata.get("name") or "")
    container_statuses = status.get("containerStatuses") or []
    total = len(container_statuses) or len(spec.get("containers") or [])
    ready = sum(1 for container in container_statuses if container.get("ready"))
    diagnostics: list[ComponentDiagnostic] = []
    for container in container_statuses[:3]:
        if container.get("ready"):
            continue
        waiting = (container.get("state") or {}).get("waiting")
        terminated = (container.get("state") or {}).get("terminated")
        state = waiting or terminated
        if state and state.get("reason"):
            diagnostics.append(
                ComponentDiagnostic(
                    severity="warning",
                    code="CONTAINER_NOT_READY",
                    message=str(state["reason"]),
                    resource=f"pod/{name}",
                )
            )
    return ready, total, diagnostics


def _component(
    name: str,
    resources: list[dict[str, Any]],
    *,
    source: str = "llmd",
) -> ClusterStackComponent:
    pods = [item for item in resources if item.get("kind") == "Pod"]
    workloads = [item for item in resources if item.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}]
    if not pods and not workloads:
        return ClusterStackComponent(name=name, status="missing", source="none")
    readiness = [_pod_ready(item) for item in pods] if pods else [_resource_ready(item) for item in workloads]
    desired_values = [value for _, value, _ in readiness if value is not None]
    ready_values = [value for value, _, _ in readiness if value is not None]
    diagnostics = [diagnostic for _, _, entries in readiness for diagnostic in entries]
    desired = sum(desired_values) if desired_values else None
    ready = sum(ready_values) if ready_values else None
    status = "ready" if desired is not None and ready == desired and desired > 0 else "progressing"
    return ClusterStackComponent(
        name=name,
        status=status,
        ready=ready,
        desired=desired,
        source=source,
        diagnostics=diagnostics,
    )


def _matching(
    items: list[dict[str, Any]],
    include: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    matched = []
    for item in items:
        name = str((item.get("metadata") or {}).get("name") or "").lower()
        if any(token in name for token in include) and not any(token in name for token in exclude):
            matched.append(item)
    return matched


def _by_label_name(items: list[dict[str, Any]], value: str) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if ((item.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/name") == value
    ]


def _local_or_external_component(
    name: str,
    local: list[dict[str, Any]],
    cluster: list[dict[str, Any]],
) -> ClusterStackComponent:
    """Build a component that may be satisfied by a shared cluster-scoped resource.

    The installer deliberately reuses pre-existing Prometheus operators and
    node-exporters that live in other namespaces (disabling the chart's own copy
    to avoid conflicts). Those shared resources are reported as ``external``
    rather than ``missing``.
    """
    component = _component(name, local or cluster, source="llmd" if local else "cluster")
    if component.status == "ready" and not local:
        component.status = "external"
    return component


def _dashboard_component(
    dashboards: list[dict[str, Any]],
    expected_dashboards: list[str] | None,
) -> ClusterStackComponent:
    present_names = {str((item.get("metadata") or {}).get("name") or "") for item in dashboards}
    if expected_dashboards:
        expected = list(expected_dashboards)
        present = [name for name in expected if name in present_names]
        missing = [name for name in expected if name not in present_names]
        ready = len(present)
        desired = len(expected)
        if desired and ready == desired:
            status = "ready"
            diagnostics: list[ComponentDiagnostic] = []
        elif ready == 0:
            status = "missing"
            diagnostics = [
                ComponentDiagnostic(
                    severity="warning",
                    code="DASHBOARDS_MISSING",
                    message=f"No llm-d Grafana dashboards are loaded (expected {desired})",
                )
            ]
        else:
            status = "degraded"
            diagnostics = [
                ComponentDiagnostic(
                    severity="warning",
                    code="DASHBOARDS_PARTIAL",
                    message=f"{len(missing)} of {desired} llm-d dashboards are missing: {', '.join(sorted(missing))}",
                )
            ]
        return ClusterStackComponent(
            name="llm_d_dashboards",
            status=status,
            kind="configmap",
            ready=ready,
            desired=desired,
            source="llmd" if ready else "none",
            diagnostics=diagnostics,
        )
    llm_d_names = sorted(name for name in present_names if name.startswith("llm-d-"))
    return ClusterStackComponent(
        name="llm_d_dashboards",
        status="ready" if llm_d_names else "missing",
        kind="configmap",
        ready=len(llm_d_names),
        desired=None,
        source="llmd" if llm_d_names else "none",
        diagnostics=(
            []
            if llm_d_names
            else [
                ComponentDiagnostic(
                    severity="warning",
                    code="DASHBOARDS_MISSING",
                    message="No llm-d Grafana dashboard ConfigMaps were found",
                )
            ]
        ),
    )


async def discover_cluster_stack(
    namespace: str,
    runner: CommandRunner,
    *,
    active_operation_id: str | None = None,
    expected_dashboards: list[str] | None = None,
) -> ClusterStackStatusResponse:
    observed_at = datetime.now(UTC)
    try:
        context_result = await runner.run(["kubectl", "config", "current-context"])
        context = context_result.stdout.strip() if context_result.returncode == 0 else None
        reachable = await runner.run(["kubectl", "cluster-info"])
    except CommandNotFoundError as error:
        raise ClusterStackError("PREFLIGHT_FAILED", str(error), status_code=400) from error
    except TimeoutError as error:
        raise ClusterStackError(
            "COMMAND_TIMEOUT",
            "Timed out while querying Kubernetes",
            retryable=True,
            status_code=504,
        ) from error

    if reachable.returncode != 0:
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context=context, reachable=False, platform="unknown"),
            namespace=namespace,
            release=HelmReleaseSummary(),
            status="unreachable",
            message=reachable.stderr.strip() or "Kubernetes cluster is unreachable",
            observed_at=observed_at,
            active_operation_id=active_operation_id,
        )

    openshift_result = await runner.run(["kubectl", "get", "clusterversion", "-o", "json"])
    platform = "openshift" if openshift_result.returncode == 0 else "kubernetes"
    if platform == "openshift":
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context=context, reachable=True, platform="openshift"),
            namespace=namespace,
            release=HelmReleaseSummary(),
            status="unsupported",
            message="OpenShift uses built-in user workload monitoring",
            observed_at=observed_at,
            active_operation_id=active_operation_id,
        )

    helm = await runner.run(["helm", "status", "llmd", "-n", namespace, "-o", "json"])
    helm_payload = _json(helm, {})
    helm_error = f"{helm.stdout}\n{helm.stderr}".lower()
    release_missing = helm.returncode != 0 and any(marker in helm_error for marker in _RELEASE_NOT_FOUND)
    chart = None
    if helm.returncode == 0:
        helm_list = await runner.run(["helm", "list", "-n", namespace, "--filter", "^llmd$", "-o", "json"])
        listed_releases = _json(helm_list, [])
        if listed_releases:
            chart = listed_releases[0].get("chart")
    release = HelmReleaseSummary(
        status=((helm_payload.get("info") or {}).get("status") if helm_payload else None),
        chart=chart,
        revision=helm_payload.get("version"),
    )

    resources_result = await runner.run(
        [
            "kubectl",
            "get",
            "pods,deployments,statefulsets,daemonsets,services",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/instance=llmd",
            "-o",
            "json",
        ]
    )
    resources = (_json(resources_result, {}) or {}).get("items", [])
    monitoring_result = await runner.run(
        [
            "kubectl",
            "get",
            "pods,statefulsets",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name in (prometheus,alertmanager)",
            "-o",
            "json",
        ]
    )
    monitoring_resources = (_json(monitoring_result, {}) or {}).get("items", [])
    operator_result = await runner.run(
        [
            "kubectl",
            "get",
            "pods,deployments",
            "--all-namespaces",
            "-l",
            "app.kubernetes.io/name=prometheus-operator",
            "-o",
            "json",
        ]
    )
    operator_resources = (_json(operator_result, {}) or {}).get("items", [])
    if not operator_resources:
        # Existing operators may not carry the standard label; fall back to
        # matching pod/deployment names, mirroring the installer's detection.
        operator_any = await runner.run(["kubectl", "get", "pods,deployments", "--all-namespaces", "-o", "json"])
        operator_resources = _matching(
            (_json(operator_any, {}) or {}).get("items", []),
            ("prometheus-stack-operator", "prometheus-operator"),
        )
    node_exporter_result = await runner.run(
        [
            "kubectl",
            "get",
            "pods,daemonsets",
            "--all-namespaces",
            "-l",
            "app=node-exporter",
            "-o",
            "json",
        ]
    )
    node_exporter_resources = (_json(node_exporter_result, {}) or {}).get("items", [])
    if not node_exporter_resources:
        node_exporter_any = await runner.run(["kubectl", "get", "pods,daemonsets", "--all-namespaces", "-o", "json"])
        node_exporter_resources = _matching(
            (_json(node_exporter_any, {}) or {}).get("items", []),
            ("node-exporter", "nodeexporter"),
        )
    dashboards_result = await runner.run(
        ["kubectl", "get", "configmaps", "-n", namespace, "-l", "grafana_dashboard=1", "-o", "json"]
    )
    dashboards = (_json(dashboards_result, {}) or {}).get("items", [])

    crd_names = (
        "servicemonitors.monitoring.coreos.com",
        "podmonitors.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
    )
    crd_ready = 0
    for crd_name in crd_names:
        result = await runner.run(["kubectl", "get", "crd", crd_name, "-o", "name"])
        crd_ready += int(result.returncode == 0)

    prometheus = _component("prometheus", _by_label_name(monitoring_resources, "prometheus"))
    grafana = _component("grafana", _matching(resources, ("grafana",)))
    local_operator_resources = _matching(resources, ("prometheus-stack-operator", "prometheus-operator"))
    operator = _local_or_external_component("prometheus_operator", local_operator_resources, operator_resources)
    alertmanager = _component(
        "alertmanager",
        _by_label_name(monitoring_resources, "alertmanager"),
    )
    kube_state = _component(
        "kube_state_metrics",
        _matching(resources, ("kube-state-metrics",)),
    )
    local_node_exporter = _matching(resources, ("node-exporter", "nodeexporter"))
    node_exporter = _local_or_external_component("node_exporter", local_node_exporter, node_exporter_resources)
    dashboard_component = _dashboard_component(dashboards, expected_dashboards)
    crds = ClusterStackComponent(
        name="monitoring_crds",
        status="ready" if crd_ready == len(crd_names) else "missing",
        kind="crd",
        ready=crd_ready,
        desired=len(crd_names),
        source="cluster" if crd_ready else "none",
    )
    components = [prometheus, grafana, operator, alertmanager, kube_state, node_exporter, dashboard_component, crds]

    has_core_resources = prometheus.status != "missing" or grafana.status != "missing"
    dashboards_ready = not expected_dashboards or dashboard_component.status == "ready"
    # These must all report "ready" (or "external" for shared/reused
    # cluster-scoped resources) for the stack as a whole to be "ready" --
    # previously alertmanager/kube_state_metrics/node_exporter were left out
    # of this check entirely, so e.g. a missing/degraded node-exporter
    # silently kept the aggregate status "ready" even though the
    # ready/desired component count (surfaced in the UI as "7/8") showed
    # something was actually wrong.
    core_components = (prometheus, grafana, operator, alertmanager, kube_state, node_exporter)
    if release_missing and not has_core_resources:
        status = "absent"
        message = "Monitoring stack is not installed"
    elif active_operation_id or any(component.status == "progressing" for component in core_components):
        status = "installing"
        message = "Monitoring stack is becoming ready"
    elif (
        release.status == "deployed"
        and prometheus.status == "ready"
        and grafana.status == "ready"
        and operator.status in {"ready", "external"}
        and alertmanager.status in {"ready", "external"}
        and kube_state.status in {"ready", "external"}
        and node_exporter.status in {"ready", "external"}
        and crds.status == "ready"
        and dashboards_ready
    ):
        status = "ready"
        message = "Monitoring stack is ready"
    else:
        status = "degraded"
        message = "Monitoring stack requires attention"

    return ClusterStackStatusResponse(
        cluster=ClusterSummary(context=context, reachable=True, platform="kubernetes"),
        namespace=namespace,
        release=release,
        status=status,
        message=message,
        components=components,
        active_operation_id=active_operation_id,
        observed_at=observed_at,
    )
