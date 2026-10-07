"""Per-deployment observability management for the Deploy module.

A deployment's monitoring is implemented as Prometheus Operator
``ServiceMonitor``/``PodMonitor`` resources created in the deployment's own
namespace.  The central Prometheus stack (installed once per cluster by the
cluster observability feature) is configured to scrape every ServiceMonitor and
PodMonitor in every namespace, so creating these resources is all that is
needed to start collecting metrics for a deployment.

Lifecycle:
  install   -> apply the ServiceMonitor/PodMonitor + state marker (enabled)
  enable    -> re-apply the monitors (turn scraping back on)
  disable   -> delete the monitors but keep the marker (stop scraping)
  uninstall -> delete the monitors and the marker (full cleanup)

Derived state:
  enabled  : at least one managed monitor exists in the namespace
  disabled : no monitors but the marker records "disabled"
  absent   : no monitors and no marker
  unreachable : cluster kubeconfig cannot be reached
"""

from __future__ import annotations

import asyncio

import yaml

from llm_d_bench.deploy.executions import (
    DeploymentExecutionNotFoundError,
    get_execution_context,
    list_execution_contexts,
)
from llm_d_bench.utils.kubernetes import (
    cluster_info,
    list_resources,
    scoped_runner,
)

# The central kube-prometheus-stack is installed into this namespace once per
# cluster (see the cluster observability "install" flow).  Its Prometheus is
# configured with empty monitor selectors, so it scrapes every ServiceMonitor
# and PodMonitor in every namespace automatically.
CENTRAL_NAMESPACE = "llm-d-monitoring"

EPP_METRICS_PORT = "http-metrics"
MODELSERVER_PORT = "modelserver"
# How often the central Prometheus scrapes each managed monitor. Kept short
# (5s) so the Flow Map's request/token rates track live traffic instead of a
# slow 15s average. Prometheus rate queries need two samples, so the profiling
# service derives its lookback window (RATE_WINDOW) from this interval.
SCRAPE_INTERVAL = "5s"
# vLLM pod roles scraped for deployment monitoring and profiling. Prefill is
# included so the Flow Map can report its queue depth and request/token rates
# in disaggregated (prefill/decode) deployments.
MODELSERVER_ROLES = ("decode", "prefill")

MARKER_NAME = "llm-d-prism-monitoring"
EPP_MONITOR_NAME = "llm-d-prism-epp-monitor"
MODELSERVER_MONITOR_NAME = "llm-d-prism-modelserver-monitor"

# The EPP serves /metrics behind controller-runtime's authn/authz filter, so an
# anonymous scrape gets 401. Prometheus must present a bearer token; its pod's
# projected service-account token is used because the kubelet rotates it (a
# Secret-stored token would silently expire and break scraping).
SA_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - a path, not a secret
# Cluster-scoped RBAC that makes the authenticated scrape actually succeed:
#   * the EPP's own ServiceAccount must be able to run TokenReview and
#     SubjectAccessReview, otherwise its filter fails closed (500) for every
#     token it is handed;
#   * Prometheus's ServiceAccount must be authorized for the non-resource URL
#     /metrics, otherwise the SubjectAccessReview denies it (403).
METRICS_READER_ROLE = "llm-d-prism-metrics-reader"
METRICS_READER_BINDING = "llm-d-prism-metrics-reader"
AUTH_DELEGATOR_ROLE = "system:auth-delegator"
DEFAULT_PROMETHEUS_SERVICE_ACCOUNT = "llmd-kube-prometheus-stack-prometheus"

# Labels applied to every resource we manage so they can be discovered and
# removed as a group without colliding with user-created monitors.
MANAGED_LABELS = {
    "app.kubernetes.io/managed-by": "llm-d-prism",
    "llm-d-prism.io/monitoring": "deployment",
}
MANAGED_SELECTOR = ",".join(f"{k}={v}" for k, v in MANAGED_LABELS.items())


class DeploymentMonitoringError(Exception):
    """Raised for user-facing monitoring failures with a stable error code."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _runner(cluster_id: str | None):
    if cluster_id is None:
        raise DeploymentMonitoringError("cluster_required", "a cluster id is required", 400)
    return scoped_runner(cluster_id)


def _deployment_target(execution_id, cluster_id=None):
    """Resolve the cluster and namespace backing a deployment execution.

    Deployment facts come from Deploy's public execution facade; this module
    never reads Deploy's run store, and callers identify a deployment only by
    ``execution_id``.
    """
    try:
        context = get_execution_context(execution_id)
    except DeploymentExecutionNotFoundError as error:
        raise DeploymentMonitoringError(
            "deployment_not_found",
            f"deployment execution '{execution_id}' not found",
            404,
        ) from error
    if cluster_id and context.cluster_id and context.cluster_id != str(cluster_id):
        raise DeploymentMonitoringError(
            "cluster_mismatch",
            "deployment does not belong to the selected cluster",
            404,
        )
    resolved_cluster_id = str(cluster_id or "") or (context.cluster_id or "")
    if not resolved_cluster_id:
        raise DeploymentMonitoringError("cluster_required", "a cluster id is required", 400)
    if not context.namespace:
        raise DeploymentMonitoringError("no_namespace", "deployment has no namespace", 400)
    return context, context.namespace, resolved_cluster_id


def _cluster_deployment_targets(cluster_id):
    """Yield (execution_id, namespace) for every deployment in a cluster."""
    return [
        (context.execution_id, context.namespace)
        for context in list_execution_contexts(cluster_id=cluster_id)
        if context.namespace
    ]


async def _central_stack_ready(cluster_id) -> bool:
    pods = await list_resources("pods", namespace=CENTRAL_NAMESPACE, cluster_id=cluster_id)
    return any((p.get("status") or {}).get("phase") == "Running" for p in pods)


def _is_epp_service(name: str, labels: dict) -> bool:
    if name.endswith("-epp"):
        return True
    app_name = str(labels.get("app.kubernetes.io/name") or "")
    if "epp" in app_name:
        return True
    return any("epp" in str(value) for value in labels.values())


async def _discover_epp_service(namespace, cluster_id):
    services = await list_resources("services", namespace=namespace, cluster_id=cluster_id)
    for item in services:
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "")
        labels = dict(metadata.get("labels") or {})
        if _is_epp_service(name, labels):
            return {
                "name": name,
                "labels": labels,
                "service_account": await _discover_epp_service_account(namespace, cluster_id, item),
            }
    return None


async def _discover_epp_service_account(namespace, cluster_id, service) -> str | None:
    """Find the ServiceAccount the EPP pods run as.

    That identity — not Prometheus's — is what performs the TokenReview for an
    incoming /metrics scrape, so it is the one that needs the auth-delegator
    role.
    """
    selector = ",".join(f"{key}={value}" for key, value in ((service.get("spec") or {}).get("selector") or {}).items())
    pods = await list_resources("pods", namespace=namespace, selector=selector or None, cluster_id=cluster_id)
    for pod in pods:
        account = str((pod.get("spec") or {}).get("serviceAccountName") or "")
        if account and account != "default":
            return account
    return None


async def _discover_prometheus_service_account(cluster_id) -> str:
    """Resolve the central Prometheus ServiceAccount that performs the scrape."""
    for item in await list_resources("prometheuses", namespace=CENTRAL_NAMESPACE, cluster_id=cluster_id):
        account = str((item.get("spec") or {}).get("serviceAccountName") or "")
        if account:
            return account
    return DEFAULT_PROMETHEUS_SERVICE_ACCOUNT


def _metrics_reader_manifests(prometheus_service_account: str) -> list[dict]:
    """Authorize Prometheus for the non-resource URL ``/metrics``.

    Cluster-scoped and shared by every monitored deployment, so it is applied
    idempotently on enable and deliberately never removed on disable.
    """
    return [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": METRICS_READER_ROLE, "labels": dict(MANAGED_LABELS)},
            "rules": [{"nonResourceURLs": ["/metrics"], "verbs": ["get"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": METRICS_READER_BINDING, "labels": dict(MANAGED_LABELS)},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": METRICS_READER_ROLE,
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": prometheus_service_account,
                    "namespace": CENTRAL_NAMESPACE,
                }
            ],
        },
    ]


def _epp_auth_delegator_binding_name(namespace: str) -> str:
    return f"llm-d-prism-epp-auth-delegator-{namespace}"


def _epp_auth_delegator_binding(namespace: str, service_account: str) -> dict:
    """Let the EPP ServiceAccount run TokenReview/SubjectAccessReview.

    Without it the EPP's metrics filter cannot verify any presented token and
    fails closed, so the scrape never succeeds regardless of Prometheus's own
    permissions.
    """
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": _epp_auth_delegator_binding_name(namespace),
            "labels": {**MANAGED_LABELS, "llm-d-prism.io/namespace": namespace},
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": AUTH_DELEGATOR_ROLE,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": service_account,
                "namespace": namespace,
            }
        ],
    }


def _match_labels(service: dict) -> dict:
    """Build a stable label selector for the EPP service.

    The EPP service is labelled ``app.kubernetes.io/name: <release>-epp`` plus a
    version label.  Match only on the stable name component so the monitor
    keeps working across chart version bumps.
    """
    labels = dict(service.get("labels") or {})
    name = labels.get("app.kubernetes.io/name")
    if name:
        return {"app.kubernetes.io/name": name}
    app = labels.get("app")
    if app:
        return {"app": app}
    return labels


def _epp_service_monitor(namespace: str, epp_service: dict) -> dict:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": EPP_MONITOR_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "spec": {
            "endpoints": [
                {
                    "port": EPP_METRICS_PORT,
                    "path": "/metrics",
                    "interval": SCRAPE_INTERVAL,
                    "bearerTokenFile": SA_TOKEN_FILE,
                }
            ],
            "jobLabel": epp_service["name"],
            "namespaceSelector": {"matchNames": [namespace]},
            "selector": {"matchLabels": _match_labels(epp_service)},
        },
    }


def _modelserver_pod_monitor(namespace: str) -> dict:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {
            "name": MODELSERVER_MONITOR_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "spec": {
            "selector": {
                "matchExpressions": [
                    {
                        "key": "llm-d.ai/role",
                        "operator": "In",
                        "values": list(MODELSERVER_ROLES),
                    }
                ]
            },
            "podMetricsEndpoints": [
                {
                    "port": MODELSERVER_PORT,
                    "path": "/metrics",
                    "interval": SCRAPE_INTERVAL,
                }
            ],
        },
    }


def _marker_configmap(namespace: str, state: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": MARKER_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "data": {"state": state},
    }


async def _managed_monitors(namespace, cluster_id):
    sms, pms = await asyncio.gather(
        list_resources("servicemonitors", namespace=namespace, selector=MANAGED_SELECTOR, cluster_id=cluster_id),
        list_resources("podmonitors", namespace=namespace, selector=MANAGED_SELECTOR, cluster_id=cluster_id),
    )
    sm_names = [str((m.get("metadata") or {}).get("name") or "") for m in sms]
    pm_names = [str((m.get("metadata") or {}).get("name") or "") for m in pms]
    return sm_names, pm_names


async def _marker_state(namespace, cluster_id):
    cms = await list_resources("configmaps", namespace=namespace, selector=MANAGED_SELECTOR, cluster_id=cluster_id)
    for cm in cms:
        name = str((cm.get("metadata") or {}).get("name") or "")
        if name == MARKER_NAME:
            return str((cm.get("data") or {}).get("state") or "").lower() or "enabled"
    return None


async def _namespace_status(
    cluster_id,
    namespace,
    execution_id=None,
    *,
    reachable=None,
    stack_ready=None,
):
    if reachable is None:
        reachable = (await cluster_info(cluster_id)).returncode == 0

    if not reachable:
        return {
            "execution_id": execution_id,
            "namespace": namespace,
            "cluster_reachable": False,
            "stack_ready": False,
            "status": "unreachable",
            "enabled": False,
            "epp_service": None,
            "servicemonitors": [],
            "podmonitors": [],
            "message": "cluster is not reachable",
        }

    if stack_ready is None:
        stack_ready = await _central_stack_ready(cluster_id)

    (sm_names, pm_names), marker, epp = await asyncio.gather(
        _managed_monitors(namespace, cluster_id),
        _marker_state(namespace, cluster_id),
        _discover_epp_service(namespace, cluster_id),
    )

    enabled = bool(sm_names or pm_names)
    if enabled:
        status = "enabled"
    elif marker == "disabled":
        status = "disabled"
    else:
        status = "absent"

    if status == "enabled":
        message = "monitoring enabled"
    elif status == "disabled":
        message = "monitoring disabled"
    else:
        message = "monitoring not installed"

    return {
        "execution_id": execution_id,
        "namespace": namespace,
        "cluster_reachable": reachable,
        "stack_ready": stack_ready,
        "status": status,
        "enabled": enabled,
        "epp_service": epp,
        "servicemonitors": sm_names,
        "podmonitors": pm_names,
        "message": message,
    }


async def get_status(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    return await _namespace_status(cluster_id, namespace, execution_id)


async def get_cluster_status(cluster_id):
    cluster_info_result, central_stack_ready = await asyncio.gather(
        cluster_info(cluster_id),
        _central_stack_ready(cluster_id),
    )
    reachable = cluster_info_result.returncode == 0
    stack_ready = central_stack_ready if reachable else False

    items = await asyncio.gather(
        *(
            _namespace_status(
                cluster_id,
                namespace,
                execution_id,
                reachable=reachable,
                stack_ready=stack_ready,
            )
            for execution_id, namespace in _cluster_deployment_targets(cluster_id)
        )
    )
    return {
        "cluster_id": cluster_id,
        "cluster_reachable": reachable,
        "stack_ready": stack_ready,
        "items": list(items),
    }


async def _apply_manifests(cluster_id, namespace, epp_service):
    manifests = []
    if epp_service:
        manifests.append(_epp_service_monitor(namespace, epp_service))
        # The EPP scrape is authenticated, so it only works once both sides of
        # the authn/authz handshake are permitted. Applied together with the
        # monitor so enabling monitoring is a single, complete operation.
        if epp_service.get("service_account"):
            manifests.append(_epp_auth_delegator_binding(namespace, epp_service["service_account"]))
            manifests.extend(_metrics_reader_manifests(await _discover_prometheus_service_account(cluster_id)))
    manifests.append(_modelserver_pod_monitor(namespace))
    manifests.append(_marker_configmap(namespace, "enabled"))
    document = yaml.safe_dump_all(manifests, sort_keys=False)

    result = await _runner(cluster_id).run(["kubectl", "apply", "-f", "-"], input=document, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "kubectl apply failed").strip()
        raise DeploymentMonitoringError("apply_failed", detail, 500)


async def _delete_monitors(cluster_id, namespace):
    sm_names, pm_names = await _managed_monitors(namespace, cluster_id)
    targets = [f"servicemonitor/{name}" for name in sm_names]
    targets += [f"podmonitor/{name}" for name in pm_names]
    if not targets:
        return
    result = await _runner(cluster_id).run(
        ["kubectl", "delete", *targets, "-n", namespace, "--ignore-not-found=true"],
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "kubectl delete failed").strip()
        raise DeploymentMonitoringError("delete_failed", detail, 500)


async def _delete_epp_rbac(cluster_id, namespace):
    """Drop the namespace's EPP auth-delegator binding.

    The shared metrics-reader role is left in place because other monitored
    namespaces still depend on it.
    """
    await _runner(cluster_id).run(
        [
            "kubectl",
            "delete",
            "clusterrolebinding",
            _epp_auth_delegator_binding_name(namespace),
            "--ignore-not-found=true",
        ],
        timeout=30,
    )


async def _apply_marker(cluster_id, namespace, state):
    document = yaml.safe_dump(_marker_configmap(namespace, state), sort_keys=False)
    result = await _runner(cluster_id).run(["kubectl", "apply", "-f", "-"], input=document, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "kubectl apply failed").strip()
        raise DeploymentMonitoringError("apply_failed", detail, 500)


async def _delete_marker(cluster_id, namespace):
    result = await _runner(cluster_id).run(
        [
            "kubectl",
            "delete",
            "configmap",
            MARKER_NAME,
            "-n",
            namespace,
            "--ignore-not-found=true",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "kubectl delete failed").strip()
        raise DeploymentMonitoringError("delete_failed", detail, 500)


async def _enable(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)

    reachable = (await cluster_info(cluster_id)).returncode == 0
    if not reachable:
        raise DeploymentMonitoringError("cluster_unreachable", "cluster is not reachable", 409)
    stack_ready = await _central_stack_ready(cluster_id)
    if not stack_ready:
        raise DeploymentMonitoringError(
            "stack_not_ready",
            "the central monitoring stack is not installed in this cluster; "
            "install it from the cluster Observability panel first",
            409,
        )

    epp = await _discover_epp_service(namespace, cluster_id)
    await _apply_manifests(cluster_id, namespace, epp)
    return await _namespace_status(
        cluster_id,
        namespace,
        execution_id,
        reachable=reachable,
        stack_ready=stack_ready,
    )


async def install(execution_id, cluster_id=None):
    return await _enable(execution_id, cluster_id)


async def enable(execution_id, cluster_id=None):
    return await _enable(execution_id, cluster_id)


async def disable(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    if (await cluster_info(cluster_id)).returncode != 0:
        raise DeploymentMonitoringError("cluster_unreachable", "cluster is not reachable", 409)
    await _delete_monitors(cluster_id, namespace)
    await _delete_epp_rbac(cluster_id, namespace)
    await _apply_marker(cluster_id, namespace, "disabled")
    return await _namespace_status(cluster_id, namespace, execution_id)


async def uninstall(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    if (await cluster_info(cluster_id)).returncode != 0:
        raise DeploymentMonitoringError("cluster_unreachable", "cluster is not reachable", 409)
    await _delete_monitors(cluster_id, namespace)
    await _delete_epp_rbac(cluster_id, namespace)
    await _delete_marker(cluster_id, namespace)
    return await _namespace_status(cluster_id, namespace, execution_id)
