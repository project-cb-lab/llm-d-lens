"""Cluster management and Kubernetes overview orchestration."""

from __future__ import annotations

from llm_d_bench.monitoring.prometheus import query_vector as _prometheus_query

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from typing import Any
from uuid import uuid4

import httpx
import yaml

from llm_d_bench.cluster import registry, sessions
from llm_d_bench.cluster.errors import ClusterOverviewError
from llm_d_bench.cluster.settings import cluster_settings
from llm_d_bench.monitoring.cluster_stack.discovery import discover_cluster_stack
from llm_d_bench.monitoring.cluster_stack.errors import ClusterStackError
from llm_d_bench.utils.kubernetes import PortForwardError, ensure_port_forward, run_kubectl, scoped_runner
from llm_d_bench.utils.shell import CommandResult
from llm_d_bench.versions import stack as stack_profile

logger = logging.getLogger(__name__)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coalesce_number(*values: Any) -> int:
    for value in values:
        number = _int(value, default=0)
        if number:
            return number
    return 0


async def cluster_is_ready(cluster: registry.Cluster) -> bool:
    """Probe a cluster's API server to determine whether it is reachable/ready."""
    kubeconfig = registry.kubeconfig_path(cluster.id)
    if not kubeconfig.is_file():
        return False
    try:
        result = await run_kubectl(["get", "--raw=/readyz"], cluster_id=cluster.id, timeout=5)
    except (FileNotFoundError, TimeoutError):
        return False
    return result.returncode == 0 and "ok" in (result.stdout or "").lower()


async def set_node_maintenance(cluster: registry.Cluster, names: list[str], *, disabled: bool) -> list[dict[str, Any]]:
    """Cordon (``disabled=True``) or uncordon (``disabled=False``) worker nodes.

    Cordoning only flips ``spec.unschedulable``, which stops the scheduler
    from placing *new* Pods on the node; it deliberately does not evict Pods
    already running there (that would be ``kubectl drain``, a separate and
    more disruptive operation not requested here). Control-plane nodes are
    rejected without touching the cluster, matching the worker-only
    restriction enforced in the UI.
    """
    nodes_result = await run_kubectl(["get", "nodes", "-o", "json"], cluster_id=cluster.id, timeout=30)
    nodes = json_output(nodes_result, "kubectl get nodes").get("items") or []
    roles = {summary["name"]: summary["role"] for summary in (_node_summary(node) for node in nodes)}

    async def _apply(name: str) -> dict[str, Any]:
        if name not in roles:
            return {"name": name, "ok": False, "error": f"Node '{name}' was not found in this cluster"}
        if roles[name] != "worker":
            return {
                "name": name,
                "ok": False,
                "error": f"Node '{name}' is a control-plane node and cannot be put into maintenance mode",
            }
        verb = "cordon" if disabled else "uncordon"
        try:
            result = await run_kubectl([verb, name], cluster_id=cluster.id, timeout=15)
        except FileNotFoundError as error:
            return {"name": name, "ok": False, "error": str(error)}
        except TimeoutError as error:
            return {"name": name, "ok": False, "error": str(error)}
        if result.returncode != 0:
            return {
                "name": name,
                "ok": False,
                "error": (result.stderr or result.stdout or f"kubectl {verb} failed").strip(),
            }
        return {"name": name, "ok": True, "schedulingDisabled": disabled}

    return list(await asyncio.gather(*(_apply(name) for name in names)))


async def list_model_secrets(cluster: registry.Cluster) -> list[dict[str, str]]:
    """List external Secret coordinates containing HF_TOKEN without exposing values.

    Deliberately does not filter namespaces by name/prefix: a deployment or
    the Create Cluster wizard's Model Cache step can put its namespace under
    any user-chosen name (including a custom ``namespace_policy.prefix`` --
    see ``build_namespace_factory``), so prefix-based filtering is
    unreliable and previously hid legitimate secrets (e.g. one in
    ``llm-d-bench-storage-<cluster id>``). Every HF_TOKEN secret in every
    namespace this cluster's kubeconfig can see is returned; ``kubectl get
    secrets -A`` already only returns what the credentials are authorized
    to list.
    """
    result = await run_kubectl(["get", "secrets", "-A", "-o", "json"], cluster_id=cluster.id, timeout=30)
    payload = json_output(result, "model credential discovery")
    secrets = []
    for item in payload.get("items", []):
        metadata = item.get("metadata") or {}
        namespace = str(metadata.get("namespace") or "default")
        if "HF_TOKEN" not in (item.get("data") or {}):
            continue
        secrets.append(
            {
                "namespace": namespace,
                "name": str(metadata.get("name") or ""),
                "createdAt": str(metadata.get("creationTimestamp") or ""),
            }
        )
    return sorted((item for item in secrets if item["name"]), key=lambda item: (item["namespace"], item["name"]))


def json_output(result: CommandResult, label: str) -> Any:
    if result.returncode != 0:
        raise ClusterOverviewError(f"{label}: {result.stderr or result.stdout or 'command failed'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ClusterOverviewError(f"{label} returned invalid JSON") from None


def namespace_summary(namespaces: list[Any], deployments: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for namespace in namespaces:
        name = str((namespace.get("metadata") or {}).get("name") or "unknown")
        items = [item for item in deployments if (item.get("metadata") or {}).get("namespace") == name]
        ready = sum(_int((item.get("status") or {}).get("readyReplicas")) for item in items)
        result.append(
            {
                "name": name,
                "deployments": [
                    {
                        "name": str((item.get("metadata") or {}).get("name") or "unknown"),
                        "desired": _int((item.get("spec") or {}).get("replicas")),
                        "ready": _int((item.get("status") or {}).get("readyReplicas")),
                        "available": _int((item.get("status") or {}).get("availableReplicas")),
                    }
                    for item in items
                ],
                "ready": ready,
            }
        )
    return result


def _matches_hardware_matchers(item: Any, matchers: Any) -> bool:
    """True when a workload matches any driver DaemonSet matcher from a profile."""
    metadata = item.get("metadata") or {}
    name = str(metadata.get("name") or "").lower()
    namespace = str(metadata.get("namespace") or "")
    labels = {str(key): str(value) for key, value in (metadata.get("labels") or {}).items()}

    def condition(key: str, value: str) -> bool:
        if key == "name_contains":
            return value.lower() in name
        if key == "name":
            return value.lower() == name
        if key == "namespace":
            return value == namespace
        if key == "label":
            label_key, _, label_value = value.partition("=")
            return labels.get(label_key) == label_value
        return False

    return any(
        matcher and all(condition(key, str(value)) for key, value in matcher.items())
        for matcher in matchers or ()
    )


def component_status(resources: list[Any]) -> dict[str, Any]:
    """Summarize GPU device plugin / resource driver workloads.

    ``intelDevicePlugin`` is retained for the existing UI contract; the additive
    ``acceleratorDevicePlugins`` map carries the same summary keyed by hardware
    profile id so new vendors need no core edit.
    """
    items: list[Any] = []
    for resource in resources:
        if isinstance(resource, dict) and isinstance(resource.get("items"), list):
            items.extend(resource["items"])
        else:
            items.append(resource)

    def matching(predicate: Any) -> list[Any]:
        return [item for item in items if predicate(item)]

    def summarize(matches: list[Any]) -> dict[str, Any]:
        desired = sum(
            _coalesce_number(
                (item.get("status") or {}).get("desiredNumberScheduled"),
                (item.get("spec") or {}).get("replicas"),
            )
            for item in matches
        )
        ready = sum(
            _coalesce_number(
                (item.get("status") or {}).get("numberReady"),
                (item.get("status") or {}).get("readyReplicas"),
            )
            for item in matches
        )
        if not matches:
            status = "missing"
        elif desired == 0 or ready >= desired:
            status = "ready"
        else:
            status = "progressing"
        return {"installed": len(matches) > 0, "ready": ready, "desired": desired, "status": status}

    intel_pattern = re.compile(r"intel.*(gpu|device|resource).*plugin|intel-gpu-resource-driver", re.IGNORECASE)

    def is_intel_plugin(item: Any) -> bool:
        name = str((item.get("metadata") or {}).get("name") or "")
        namespace = str((item.get("metadata") or {}).get("namespace") or "")
        return bool(intel_pattern.search(f"{namespace}/{name}"))

    status: dict[str, Any] = {"intelDevicePlugin": summarize(matching(is_intel_plugin))}
    accelerator_plugins: dict[str, Any] = {}
    for profile in _hardware_profiles():
        driver = profile.driver
        if driver is None:
            continue
        matchers = [matcher for mode in driver.modes.values() for matcher in mode.daemonset_matchers]
        accelerator_plugins[profile.id] = summarize(
            matching(lambda item, matchers=matchers: _matches_hardware_matchers(item, matchers))
        )
    if accelerator_plugins:
        status["acceleratorDevicePlugins"] = accelerator_plugins
    return status


_MONITORING_NAMESPACE = "llm-d-monitoring"
_PROMETHEUS_PORT = 9090

_PORT_CACHE: dict[str, tuple[int | None, float]] = {}
_MONITORING_CACHE: dict[str, tuple[dict[str, Any], float]] = {}


async def monitoring_component(cluster: registry.Cluster) -> dict[str, Any]:
    """Monitoring stack summary consistent with the observability panel with short TTL cache."""
    now = time.monotonic()
    if cluster.id in _MONITORING_CACHE:
        cached_val, expire = _MONITORING_CACHE[cluster.id]
        if now < expire:
            return cached_val

    try:
        snapshot = await discover_cluster_stack(_MONITORING_NAMESPACE, scoped_runner(cluster.id))
    except (ClusterStackError, FileNotFoundError, TimeoutError):
        res = {"installed": False, "ready": 0, "desired": 0, "status": "unreachable"}
        _MONITORING_CACHE[cluster.id] = (res, now + 10)
        return res

    components = snapshot.components or []
    ready = sum(1 for component in components if component.status in {"ready", "external"})
    res = {
        "installed": snapshot.status not in {"absent", "unreachable", "unsupported"},
        "ready": ready,
        "desired": len(components),
        "status": snapshot.status,
    }
    _MONITORING_CACHE[cluster.id] = (res, now + 20)
    return res


def _service_name(item: dict[str, Any]) -> str:
    return str((item.get("metadata") or {}).get("name") or "")


def _service_port(item: dict[str, Any], default: int = _PROMETHEUS_PORT) -> int:
    ports = (item.get("spec") or {}).get("ports") or []
    for port in ports:
        if int(port.get("port") or 0) == default:
            return default
    return int((ports[0] or {}).get("port") or default) if ports else default


async def _prometheus_local_port(cluster_id: str) -> int | None:
    now = time.monotonic()
    if cluster_id in _PORT_CACHE:
        cached_port, expire = _PORT_CACHE[cluster_id]
        if now < expire and cached_port is not None:
            return cached_port

    result = await run_kubectl(
        ["get", "services", "-n", _MONITORING_NAMESPACE, "-o", "json"], cluster_id=cluster_id, timeout=10
    )
    if result.returncode != 0:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    services = json_output(result, "kubectl get Prometheus services").get("items") or []
    service = next((item for item in services if _service_name(item).endswith("-prometheus")), None)
    if service is None:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    try:
        forward = await ensure_port_forward(
            _MONITORING_NAMESPACE,
            _service_name(service),
            _service_port(service),
            cluster_id=cluster_id,
        )
        _PORT_CACHE[cluster_id] = (forward.local_port, now + 60)
        return forward.local_port
    except PortForwardError:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None


def _prometheus_sample_value(sample: dict[str, Any]) -> float | None:
    value = sample.get("value") or []
    if len(value) < 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None




def _node_from_prometheus_labels(metric: dict[str, Any], pci_to_node: dict[str, str]) -> str:
    labels = metric.get("metric") or {}
    for key in ("node", "kubernetes_node", "hostname", "host"):
        if labels.get(key):
            return str(labels[key])
    pci = str(labels.get("pci_bdf") or labels.get("pciBusID") or labels.get("bdf") or "").strip()
    if pci and pci in pci_to_node:
        return pci_to_node[pci]
    instance = str(labels.get("instance") or "")
    return instance.split(":", 1)[0] if instance else ""


def _prometheus_device_key(metric: dict[str, Any], pci_to_node: dict[str, str]) -> tuple[str, str]:
    labels = metric.get("metric") or {}
    node = _node_from_prometheus_labels(metric, pci_to_node)
    device = str(
        labels.get("pci_bdf")
        or labels.get("pciBusID")
        or labels.get("bdf")
        or labels.get("hw_id")
        or labels.get("device")
        or labels.get("gpu")
        or labels.get("card")
        or ""
    ).strip()
    if not device:
        device = str(labels.get("instance") or labels.get("pod") or labels.get("__name__") or "")
    return (node, device)


def _latest_device_values(
    samples: list[dict[str, Any]],
    worker_node_names: set[str],
    pci_to_node: dict[str, str],
) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for sample in samples:
        value = _prometheus_sample_value(sample)
        if value is None:
            continue
        node = _node_from_prometheus_labels(sample, pci_to_node)
        if worker_node_names and (not node or node not in worker_node_names):
            continue
        key = _prometheus_device_key(sample, pci_to_node)
        if not key[1]:
            continue
        values[key] = value
    return values


def _instance_to_node(samples: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sample in samples:
        labels = sample.get("metric") or {}
        instance = str(labels.get("instance") or "")
        node = str(labels.get("nodename") or labels.get("node") or labels.get("hostname") or "")
        if instance and node:
            mapping[instance] = node
    return mapping


def _latest_node_percent_values(
    samples: list[dict[str, Any]],
    instance_to_node: dict[str, str],
    worker_node_names: set[str],
) -> dict[str, float]:
    values: dict[str, float] = {}
    for sample in samples:
        value = _prometheus_sample_value(sample)
        if value is None:
            continue
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or labels.get("nodename") or labels.get("hostname") or "")
        if not node:
            node = instance_to_node.get(str(labels.get("instance") or ""), "")
        if worker_node_names and (not node or node not in worker_node_names):
            continue
        values[node] = round(min(100.0, max(0.0, value)), 1)
    return values


async def _prometheus_hardware_metrics(
    cluster_id: str,
    worker_node_names: set[str],
    gpu_devices: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    local_port = await _prometheus_local_port(cluster_id)
    if local_port is None:
        return {}
    pci_to_node = {
        device["pciAddress"]: device["node"]
        for device in gpu_devices.values()
        if device.get("pciAddress") and device.get("node")
    }
    # Per-device queries are profile-driven (Intel XPUM vs NVIDIA DCGM names
    # differ); run one query per profile so an Intel-only profile keeps sending
    # its exact XPUM PromQL, then merge the samples by canonical metric.
    ordered_queries: list[tuple[str, str]] = []
    for canonical, expressions in _device_metric_queries().items():
        ordered_queries.extend((canonical, expression) for expression in expressions)
    ordered_queries.extend([
        ("nodeCpuUsagePercent", '100 * (1 - avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[2m])))'),
        ("nodeMemoryUsagePercent", "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))"),
        ("nodeInfo", "node_uname_info"),
    ])
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{local_port}", timeout=5.0) as client:
        results = await asyncio.gather(*(_prometheus_query(client, expression) for _, expression in ordered_queries))
    merged: dict[str, list[dict[str, Any]]] = {}
    for (canonical, _), result in zip(ordered_queries, results, strict=True):
        merged.setdefault(canonical, []).extend(result)
    sample_sets = {
        key: _latest_device_values(merged.get(key, []), worker_node_names, pci_to_node)
        for key in (
            "vramBytes",
            "vramUsedBytes",
            "gpuUsagePercent",
            "vramReadBytes",
            "vramWriteBytes",
            "vramBandwidthPercent",
        )
    }
    metrics: dict[str, Any] = {}
    size_values = sample_sets["vramBytes"]
    used_values = sample_sets["vramUsedBytes"]
    gpu_values = sample_sets["gpuUsagePercent"]
    read_values = sample_sets["vramReadBytes"]
    write_values = sample_sets["vramWriteBytes"]
    bw_values = sample_sets["vramBandwidthPercent"]

    # DCGM reports utilization as a percentage (0-100) while Intel XPUM reports
    # a ratio (0-1); normalize both to a percentage exactly once.
    gpu_util = {key: (val * 100.0 if val <= 1.0 else val) for key, val in gpu_values.items()}
    per_device_metrics: dict[tuple[str, str], dict[str, Any]] = {}
    for key, val in gpu_util.items():
        per_device_metrics.setdefault(key, {})["computeUsagePercent"] = val
    for key, val in size_values.items():
        per_device_metrics.setdefault(key, {})["vramTotalBytes"] = val
    for key, val in used_values.items():
        per_device_metrics.setdefault(key, {})["vramUsedBytes"] = val
    for key, val in read_values.items():
        per_device_metrics.setdefault(key, {})["vramReadThroughputBytes"] = val
    for key, val in write_values.items():
        per_device_metrics.setdefault(key, {})["vramWriteThroughputBytes"] = val
    for key, val in bw_values.items():
        per_device_metrics.setdefault(key, {})["vramBandwidthPercent"] = val * 100.0 if val <= 1.0 else val
    metrics["perDeviceMetrics"] = per_device_metrics
    instance_to_node = _instance_to_node(merged.get("nodeInfo", []))
    node_cpu = _latest_node_percent_values(
        merged.get("nodeCpuUsagePercent", []),
        instance_to_node,
        worker_node_names,
    )
    node_memory = _latest_node_percent_values(
        merged.get("nodeMemoryUsagePercent", []),
        instance_to_node,
        worker_node_names,
    )
    if node_cpu:
        metrics["nodeCpu"] = node_cpu
        metrics["cpuUsagePercent"] = sum(node_cpu.values()) / len(node_cpu)
    if node_memory:
        metrics["nodeMemory"] = node_memory
        metrics["memoryUsagePercent"] = sum(node_memory.values()) / len(node_memory)
    if size_values:
        vram_bytes = sum(size_values.values())
        metrics["vramBytes"] = vram_bytes
        used_bytes = sum(used_values.get(key, 0) for key in size_values)
        if used_values and vram_bytes:
            metrics["vramUsagePercent"] = used_bytes / vram_bytes * 100
        node_vram: dict[str, dict[str, float]] = {}
        for (node, _device), total_bytes in size_values.items():
            if not node:
                continue
            bucket = node_vram.setdefault(node, {"totalBytes": 0.0, "usedBytes": 0.0})
            bucket["totalBytes"] += total_bytes
            bucket["usedBytes"] += used_values.get((node, _device), 0.0)
        if node_vram:
            metrics["nodeVram"] = node_vram
    if gpu_util:
        metrics["gpuUsagePercent"] = sum(gpu_util.values()) / len(gpu_util)
        node_gpu: dict[str, list[float]] = {}
        for (node, _device), utilization in gpu_util.items():
            if node:
                node_gpu.setdefault(node, []).append(utilization)
        if node_gpu:
            metrics["nodeGpu"] = {
                node: sum(values) / len(values)
                for node, values in node_gpu.items()
                if values
            }
    return metrics


# --- Cluster registry orchestration ---


def list_clusters(*, include_drafts: bool = False) -> list[registry.Cluster]:
    return registry.list_clusters(include_drafts=include_drafts)


async def delete_cluster(cluster_id: str) -> None:
    """Delete a cluster and any deploy session registered against it.

    Draft clusters (still being assembled by the creation wizard, not yet
    finalized) skip the "still has deployments/Storage volumes" guards
    below: they are only reachable from within the wizard itself, so if the
    wizard is cancelled mid-way we still want a best-effort cleanup of
    whatever partial Storage/model-cache state it created, rather than
    surfacing a 409 the user has no UI to resolve.
    """
    cluster = registry.require_cluster(cluster_id)
    if not cluster.draft:
        if _has_available_deployments(cluster_id):
            raise ClusterOverviewError(
                "This cluster still has available deployments; clean them up before deleting the cluster.",
                status_code=409,
                code="cluster_has_available_deployments",
            )
        if _has_storage_volumes(cluster_id):
            raise ClusterOverviewError(
                "This cluster still has Storage volumes registered against it; "
                "delete them before deleting the cluster.",
                status_code=409,
                code="cluster_has_storage_volumes",
            )
    else:
        _delete_storage_volumes(cluster_id)
    # Best-effort: delete the per-cluster HF_TOKEN/model-cache namespace
    # (see the Create Cluster wizard's Model Cache step) before the
    # cluster's kubeconfig is removed below -- once it's gone there is no
    # way left to reach the cluster to clean this up.
    await _delete_storage_namespace(cluster_id)
    sessions.close_session_for_server(cluster_id)
    registry.delete_cluster(cluster_id)


async def _delete_storage_namespace(cluster_id: str) -> None:
    """Best-effort delete of ``llm-d-bench-storage-<cluster_id>``, the
    default namespace the wizard creates the HF_TOKEN secret in. Never
    raises -- an unreachable cluster or a namespace that was never created
    must not block deleting the cluster record itself."""
    from llm_d_bench.utils.kubernetes import delete_namespace

    with contextlib.suppress(Exception):
        await delete_namespace(f"llm-d-bench-storage-{cluster_id}", cluster_id=cluster_id)


def _delete_storage_volumes(cluster_id: str) -> None:
    """Best-effort cascade delete of any Storage volumes left on a draft cluster."""
    from llm_d_bench.storage.store import default_store

    store = default_store()
    for volume in [v for v in store.list() if v.cluster_id == cluster_id]:
        try:
            store.delete(volume.id)
        except Exception as error:  # noqa: BLE001 - best-effort cleanup, never block draft deletion
            logger.debug("Failed to clean up storage volume %s during draft deletion: %s", volume.id, error)
            continue


def _has_available_deployments(cluster_id: str) -> bool:
    """Return True when the cluster has any deployment still in the ready state."""
    # Lazy imports avoid a circular dependency (deploy.application imports llm_d_bench.cluster).
    from llm_d_bench.deploy.application import deployment_run_manager
    from llm_d_bench.deploy.contracts import DeploymentCaseStatus

    for run in deployment_run_manager.store.list_runs():
        if str(run.provenance.get("cluster_server_id") or "") != cluster_id:
            continue
        if any(case.status == DeploymentCaseStatus.READY for case in run.cases):
            return True
    return False


def _has_storage_volumes(cluster_id: str) -> bool:
    """Return True when the cluster still has any Storage volume registered.

    A cluster deleted while one of its volumes' PVC/PV records still exist
    leaves those records permanently unreachable: their ``cluster_id`` no
    longer resolves to a kubeconfig, so any later attempt to delete or
    inspect them fails (reproduced live: deleting a cluster first, then
    trying to delete its ``local-disk`` volume raised ``FileNotFoundError:
    cluster ... not found`` deep in ``kubectl`` plumbing). Requiring Storage
    volumes to be cleaned up first keeps every volume's ``cluster_id``
    resolvable for as long as the record exists.
    """
    # Lazy import avoids a module-level dependency cycle between
    # llm_d_bench.cluster and llm_d_bench.storage.
    from llm_d_bench.storage.store import default_store

    return any(volume.cluster_id == cluster_id for volume in default_store().list())


def create_cluster(
    name: str,
    description: str,
    kubeconfig_text: str,
    *,
    proxy_mode: str = "auto",
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    llm_d_ref: str | None = None,
    llm_d_benchmark_ref: str | None = None,
    gateway_provider: str | None = None,
    gateway_namespace: str | None = None,
    gateway_name: str | None = None,
    gateway_public_url: str | None = None,
    gateway_port: int | None = None,
    gateway_authz_host: str | None = None,
    inotify_max_user_instances: int = 8192,
    router_version: str | None = None,
    gie_version: str | None = None,
    ipp_version: str | None = None,
    draft: bool = False,
) -> tuple[registry.Cluster, sessions.ClusterSession]:
    clean_name = name.strip()
    clean_description = description.strip()
    if not clean_name:
        raise ClusterOverviewError("Cluster name is required", status_code=422, code="invalid_cluster_name")
    if not kubeconfig_text.strip():
        raise ClusterOverviewError("A kubeconfig file is required", status_code=422, code="invalid_kubeconfig")
    try:
        parsed = yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError as error:
        raise ClusterOverviewError(
            f"Uploaded kubeconfig is not valid YAML: {error}", status_code=422, code="invalid_kubeconfig"
        ) from None
    if not isinstance(parsed, dict):
        raise ClusterOverviewError(
            "Uploaded kubeconfig is not a valid kubeconfig", status_code=422, code="invalid_kubeconfig"
        )
    if proxy_mode == "custom" and not any((http_proxy, https_proxy, no_proxy)):
        raise ClusterOverviewError(
            "Custom proxy requires at least one of HTTP_PROXY/HTTPS_PROXY/NO_PROXY",
            status_code=422,
            code="invalid_proxy_config",
        )
    # llm-d component versions are fixed by the Lens stack profile; a caller may
    # not pick them (see docs/design/llm-d-stack-profile-design.md).
    current = stack_profile()
    cluster = registry.create_cluster(
        clean_name,
        clean_description,
        kubeconfig_text,
        proxy_mode=proxy_mode,
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        llm_d_ref=current.llm_d,
        llm_d_benchmark_ref=current.llm_d_benchmark,
        gateway_provider=gateway_provider,
        gateway_namespace=gateway_namespace,
        gateway_name=gateway_name,
        gateway_public_url=gateway_public_url,
        gateway_port=gateway_port,
        gateway_authz_host=gateway_authz_host,
        inotify_max_user_instances=inotify_max_user_instances,
        router_version=current.llm_d_router,
        gie_version=current.k8s_gateway_api_inference_extension,
        ipp_version=current.llm_d_inference_payload_processor,
        draft=draft,
    )
    return cluster, _register_cluster_session(cluster)


def update_cluster_settings(
    cluster_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    proxy_mode: str | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    llm_d_ref: str | None = None,
    llm_d_benchmark_ref: str | None = None,
    gateway_provider: str | None = None,
    gateway_namespace: str | None = None,
    gateway_name: str | None = None,
    gateway_public_url: str | None = None,
    gateway_port: int | None = None,
    gateway_authz_host: str | None = None,
    inotify_max_user_instances: int | None = None,
    router_version: str | None = None,
    gie_version: str | None = None,
    ipp_version: str | None = None,
    draft: bool | None = None,
) -> registry.Cluster:
    """Patch a cluster's name/description/proxy/version settings (wizard Step 2/3,
    and the Clusters overview page's "Edit cluster" action)."""
    registry.require_cluster(cluster_id)
    updates: dict[str, Any] = {}
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ClusterOverviewError("Cluster name is required", status_code=422, code="invalid_name")
        updates["name"] = clean_name
    if description is not None:
        updates["description"] = description.strip()
    if proxy_mode is not None:
        if proxy_mode not in ("auto", "custom"):
            raise ClusterOverviewError(
                "proxy mode must be 'auto' or 'custom'", status_code=422, code="invalid_proxy_config"
            )
        if proxy_mode == "custom" and not any((http_proxy, https_proxy, no_proxy)):
            raise ClusterOverviewError(
                "Custom proxy requires at least one of HTTP_PROXY/HTTPS_PROXY/NO_PROXY",
                status_code=422,
                code="invalid_proxy_config",
            )
        updates["proxy_mode"] = proxy_mode
        updates["http_proxy"] = http_proxy if proxy_mode == "custom" else None
        updates["https_proxy"] = https_proxy if proxy_mode == "custom" else None
        updates["no_proxy"] = no_proxy if proxy_mode == "custom" else None
    # llm-d component versions always follow the Lens stack profile.
    current = stack_profile()
    updates["llm_d_ref"] = current.llm_d
    updates["llm_d_benchmark_ref"] = current.llm_d_benchmark
    updates["router_version"] = current.llm_d_router
    updates["gie_version"] = current.k8s_gateway_api_inference_extension
    updates["ipp_version"] = current.llm_d_inference_payload_processor
    for key, value in (
        ("gateway_provider", gateway_provider),
        ("gateway_namespace", gateway_namespace),
        ("gateway_name", gateway_name),
        ("gateway_public_url", gateway_public_url),
        ("gateway_port", gateway_port),
        ("gateway_authz_host", gateway_authz_host),
    ):
        if value is not None:
            updates[key] = value or None
    if inotify_max_user_instances is not None:
        if inotify_max_user_instances < 1:
            raise ClusterOverviewError(
                "inotify max user instances must be at least 1",
                status_code=422,
                code="invalid_inotify_limit",
            )
        updates["inotify_max_user_instances"] = inotify_max_user_instances
    if draft is not None:
        updates["draft"] = draft
    return registry.update_cluster(cluster_id, **updates)


def record_repo_download(
    cluster_id: str,
    *,
    llm_d_ref: str | None = None,
    llm_d_repo_path: str | None = None,
    llm_d_benchmark_ref: str | None = None,
    llm_d_benchmark_repo_path: str | None = None,
) -> registry.Cluster:
    """Persist a completed llm-d/llm-d-benchmark download's ref + on-disk path.

    Called by the Software Versions step's download background task once
    ``llm_d_bench.cluster.repo_downloads.ensure_downloaded`` succeeds, so the
    resulting checkout can be resolved from the cluster record by any other
    module without re-downloading it.
    """
    registry.require_cluster(cluster_id)
    updates: dict[str, Any] = {}
    if llm_d_ref is not None:
        updates["llm_d_ref"] = llm_d_ref
        updates["llm_d_repo_path"] = llm_d_repo_path
    if llm_d_benchmark_ref is not None:
        updates["llm_d_benchmark_ref"] = llm_d_benchmark_ref
        updates["llm_d_benchmark_repo_path"] = llm_d_benchmark_repo_path
    if not updates:
        return registry.require_cluster(cluster_id)
    return registry.update_cluster(cluster_id, **updates)


def resolve_proxy_env(cluster_id: str | None) -> dict[str, str]:
    """Resolve the effective HTTP(S)_PROXY/NO_PROXY values for a cluster.

    ``mode="custom"`` returns exactly the cluster's own three values (may be
    partially empty). ``mode="auto"`` (or no cluster/unknown cluster) falls
    back to the Prism backend process's own environment, preserving
    pre-wizard behavior until real auto-detection (Phase 2 of the design
    doc) is implemented.
    """
    cluster = registry.get_cluster(cluster_id) if cluster_id else None
    if cluster is not None and cluster.proxy_mode == "custom":
        values = {
            "HTTP_PROXY": cluster.http_proxy or "",
            "HTTPS_PROXY": cluster.https_proxy or "",
            "NO_PROXY": cluster.no_proxy or "",
        }
        return {name: value for name, value in values.items() if value}
    values = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = os.environ.get(name) or os.environ.get(name.lower())
        if value:
            values[name] = value
    return values


async def create_hf_token_secret(cluster: registry.Cluster, namespace: str, name: str, token: str) -> dict[str, str]:
    """Create a standalone ``HF_TOKEN`` Secret for the wizard's Model Cache step.

    Decoupled from any Model Cache entry (see
    docs/design/cluster-creation-wizard-design.md section 4.4): the caller
    is expected to reference the resulting namespace/name via a normal
    ``TokenSourceMode.EXISTING_SECRET`` token source afterwards. Uses
    ``kubectl create`` (not ``apply``) so a name collision surfaces as an
    explicit error instead of silently overwriting an existing Secret.
    """
    namespace = namespace.strip()
    name = name.strip()
    if not namespace:
        raise ClusterOverviewError("Namespace is required", status_code=422, code="invalid_secret_namespace")
    if not name:
        raise ClusterOverviewError("Secret name is required", status_code=422, code="invalid_secret_name")
    existing = await run_kubectl(
        ["get", "secret", name, "--namespace", namespace, "--ignore-not-found", "-o", "name"],
        cluster_id=cluster.id,
        timeout=15,
    )
    if existing.returncode == 0 and (existing.stdout or "").strip():
        raise ClusterOverviewError(
            f"Secret '{name}' already exists in namespace '{namespace}'",
            status_code=409,
            code="secret_already_exists",
        )
    runner = scoped_runner(cluster.id)
    ensure_ns = await runner.run(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"], timeout=15
    )
    if ensure_ns.returncode == 0:
        await runner.run(["kubectl", "apply", "-f", "-"], input=ensure_ns.stdout, timeout=15)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "stringData": {"HF_TOKEN": token},
    }
    result = await runner.run(["kubectl", "create", "-f", "-"], input=json.dumps(manifest), timeout=15)
    if result.returncode != 0:
        raise ClusterOverviewError(
            f"Failed to create secret: {(result.stderr or result.stdout or '').strip()}",
            status_code=502,
            code="secret_create_failed",
        )
    # Record this secret's coordinates on the cluster itself so later
    # Deploy/Evaluate flows can default to reusing it (see
    # llm_d_bench/deploy/service.py's ``_default_model_secret``) instead of
    # requiring the user to re-select a secret for every deployment.
    registry.update_cluster(cluster.id, hf_token_secret_namespace=namespace, hf_token_secret_name=name)
    return {"namespace": namespace, "name": name}


def _register_cluster_session(cluster: registry.Cluster) -> sessions.ClusterSession:
    """Register a legacy cluster session so deploy/evaluate keep resolving kubeconfig."""
    sessions.close_session_for_server(cluster.id)
    source = registry.kubeconfig_path(cluster.id)
    if not source.is_file():
        raise ClusterOverviewError(
            "Cluster kubeconfig is not readable", status_code=409, code="cluster_kubeconfig_missing"
        )
    session_id = str(uuid4())
    destination = sessions.session_kubeconfig_path(session_id)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    return sessions.register_session(cluster.id, destination, session_id)


def get_cluster_session(cluster_id: str) -> tuple[registry.Cluster, sessions.ClusterSession]:
    """Resolve a cluster and an active deploy session, registering one on demand."""
    cluster = registry.require_cluster(cluster_id)
    existing = sessions.get_session_for_server(cluster.id)
    if existing is not None and sessions.session_kubeconfig_path(existing.id).is_file():
        return cluster, existing
    return cluster, _register_cluster_session(cluster)


# --- Kubernetes overview ---


_INTEL_GPU_LABEL = "intel.feature.node.kubernetes.io/gpu"
_GPU_RESOURCE_PREFIX = "gpu.intel.com/"
_MEMORY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def _cpu_cores(value: Any) -> float:
    """Parse a Kubernetes CPU quantity (e.g. "96" or "15500m") into core count."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    if text.endswith("m"):
        try:
            return float(text[:-1]) / 1000.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _memory_bytes(value: Any) -> int:
    """Parse a Kubernetes memory quantity (e.g. "192Gi", "1054604Ki", "67108864") into bytes."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    for unit, multiplier in sorted(_MEMORY_UNITS.items(), key=lambda item: len(item[0]), reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _cpu_millicores(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("m"):
        try:
            return int(float(text[:-1]))
        except ValueError:
            return 0
    if text.endswith("n"):
        try:
            return int(float(text[:-1]) / 1_000_000)
        except ValueError:
            return 0
    try:
        return int(float(text) * 1000)
    except ValueError:
        return 0


def _gpu_count(capacity: dict[str, Any]) -> int:
    """Sum extended-resource GPU device counts (e.g. gpu.intel.com/xe: "8") on a node.

    Resource prefixes and scheduling-marker suffixes come from registered
    hardware profiles (``gpu.intel.com/monitoring`` is a marker for the
    monitoring workload, not a GPU device, so it is excluded).
    """
    prefixes = _gpu_resource_prefixes()
    suffixes = _gpu_monitor_suffixes()
    total = 0
    for key, value in capacity.items():
        if not any(key.startswith(prefix) for prefix in prefixes):
            continue
        if any(key.endswith(f"/{suffix}") for suffix in suffixes):
            continue
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
    return total


_GPU_DRIVER = "gpu.intel.com"


def _hardware_profiles() -> list[Any]:
    """Registered hardware profiles; empty when discovery is unavailable."""
    try:
        from llm_d_bench.hardware.registry import all_profiles

        return all_profiles()
    except Exception:  # pragma: no cover - cluster overview must not fail on discovery errors
        return []


def _gpu_resource_prefixes() -> tuple[str, ...]:
    """Extended-resource prefixes across registered hardware, with an Intel fallback."""
    prefixes = tuple(prefix for profile in _hardware_profiles() for prefix in profile.resource_prefixes)
    return prefixes or (_GPU_RESOURCE_PREFIX,)


def _gpu_monitor_suffixes() -> tuple[str, ...]:
    """Scheduling-marker suffixes (not devices) across registered hardware."""
    suffixes = tuple(suffix for profile in _hardware_profiles() for suffix in profile.monitor_resource_suffixes)
    return suffixes or ("monitoring",)


def _gpu_device_classes() -> tuple[str, ...]:
    """DRA device classes across registered hardware, with an Intel fallback."""
    classes = tuple(device_class for profile in _hardware_profiles() for device_class in profile.device_classes)
    return classes or (_GPU_DRIVER,)


def _gpu_counts_by_profile(capacity: dict[str, Any]) -> dict[str, int]:
    """Per-profile extended-resource GPU counts for one node."""
    counts: dict[str, int] = {}
    for profile in _hardware_profiles():
        prefixes = profile.resource_prefixes or (_GPU_RESOURCE_PREFIX,)
        suffixes = profile.monitor_resource_suffixes
        total = 0
        for key, value in capacity.items():
            if not any(key.startswith(prefix) for prefix in prefixes):
                continue
            if any(key.endswith(f"/{suffix}") for suffix in suffixes):
                continue
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
        if total:
            counts[profile.id] = total
    return counts


def _resource_slice_gpu_counts_by_profile(slices: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Map node name -> {profile id: DRA device count} from ResourceSlices."""
    device_classes = {
        device_class: profile.id for profile in _hardware_profiles() for device_class in profile.device_classes
    } or {_GPU_DRIVER: "intel-xpu"}
    by_node: dict[str, dict[str, int]] = {}
    for item in slices:
        spec = item.get("spec") or {}
        profile_id = device_classes.get(spec.get("driver"))
        node_name = str(spec.get("nodeName") or "").strip()
        if profile_id is None or not node_name:
            continue
        devices = len(spec.get("devices") or [])
        bucket = by_node.setdefault(node_name, {})
        bucket[profile_id] = bucket.get(profile_id, 0) + devices
    return by_node


def _node_has_gpu_label(labels: dict[str, Any]) -> bool:
    """True when a node matches any registered hardware's presence label."""
    selectors = [profile.node_label_selector for profile in _hardware_profiles() if profile.node_label_selector]
    if selectors:
        return any(
            all(str(labels.get(key) or "").lower() == value.lower() for key, value in selector.items())
            for selector in selectors
        )
    return str(labels.get(_INTEL_GPU_LABEL) or "").lower() == "true"


_DEVICE_METRIC_KEYS = {
    "vram": "vramBytes",
    "framebuffer_used": "vramUsedBytes",
    "utilization": "gpuUsagePercent",
    "memory_read_throughput": "vramReadBytes",
    "memory_write_throughput": "vramWriteBytes",
    "memory_bandwidth_utilization": "vramBandwidthPercent",
}
# Intel XPUM metric names, used only when no registered profile declares the
# corresponding device metric (e.g. hardware discovery unavailable).
_DEFAULT_DEVICE_QUERIES = {
    "vramBytes": "hw_memory_size_bytes",
    "vramUsedBytes": "hw_memory_usage_bytes",
    "gpuUsagePercent": 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}',
    "vramReadBytes": "hw_memory_read_throughput_bytes",
    "vramWriteBytes": "hw_memory_write_throughput_bytes",
    "vramBandwidthPercent": "hw_memory_bandwidth_utilization_ratio",
}


def _device_metric_queries() -> dict[str, list[str]]:
    """Canonical device metric -> one PromQL per profile that declares it.

    Queried separately (not ``or``-combined) so an Intel-only profile keeps
    sending its exact XPUM query while a heterogeneous cluster merges results;
    the Intel XPUM defaults apply only when no profile declares the metric.
    """
    queries: dict[str, list[str]] = {canonical: [] for canonical in _DEFAULT_DEVICE_QUERIES}
    for profile in _hardware_profiles():
        telemetry = getattr(profile, "telemetry", None)
        metrics = (telemetry.device_metrics if telemetry is not None else {}) or {}
        for source, canonical in _DEVICE_METRIC_KEYS.items():
            expression = metrics.get(source)
            if expression:
                queries[canonical].append(expression)
    for canonical, fallback in _DEFAULT_DEVICE_QUERIES.items():
        if not queries[canonical]:
            queries[canonical].append(fallback)
    return queries


# Per-GPU payload keys the frontend renders. A profile that does not configure
# the corresponding ``device_metrics`` entry means the cluster cannot report it,
# so the overview advertises it as unavailable and the UI hides that section.
_DEVICE_METRIC_PAYLOAD_KEYS = {
    "utilization": "computeUsagePercent",
    "vram": "vramTotalBytes",
    "framebuffer_used": "vramUsedBytes",
    "memory_bandwidth_utilization": "vramBandwidthPercent",
    "memory_read_throughput": "vramReadThroughputBytes",
    "memory_write_throughput": "vramWriteThroughputBytes",
}


def _available_device_metrics(profile_ids: set[str]) -> list[str]:
    """Per-GPU payload keys whose PromQL the cluster's hardware profiles declare."""
    available: set[str] = set()
    for profile in _hardware_profiles():
        if profile.id not in profile_ids:
            continue
        telemetry = getattr(profile, "telemetry", None)
        metrics = (telemetry.device_metrics if telemetry is not None else {}) or {}
        for source, payload_key in _DEVICE_METRIC_PAYLOAD_KEYS.items():
            if metrics.get(source):
                available.add(payload_key)
    return sorted(available)


def _resource_slice_gpu_devices(slices: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    """Map DRA allocation identity to its node and physical PCI address."""
    devices: dict[tuple[str, str], dict[str, str]] = {}
    for item in slices:
        spec = item.get("spec") or {}
        if spec.get("driver") not in _gpu_device_classes():
            continue
        node_name = str(spec.get("nodeName") or "").strip()
        pool_name = str((spec.get("pool") or {}).get("name") or node_name).strip()
        if not node_name or not pool_name:
            continue
        for device in spec.get("devices") or []:
            device_name = str(device.get("name") or "").strip()
            pci_address = str((((device.get("attributes") or {}).get("pciAddress") or {}).get("string")) or "").strip()
            if device_name:
                devices[(pool_name, device_name)] = {"node": node_name, "pciAddress": pci_address}
    return devices


def _resource_slice_gpu_counts(slices: list[dict[str, Any]]) -> dict[str, int]:
    """Map node name -> GPU device count from Intel GPU DRA ResourceSlices.

    On DRA-managed clusters the GPUs are not exposed as node extended resources
    (``status.capacity`` is empty), so the device count must be read from the
    ``resource.k8s.io`` ResourceSlice objects published by the ``gpu.intel.com``
    driver instead. Each entry in ``spec.devices`` is one schedulable GPU.
    """
    counts: dict[str, int] = {}
    for device in _resource_slice_gpu_devices(slices).values():
        node_name = device["node"]
        counts[node_name] = counts.get(node_name, 0) + 1
    return counts


def _resource_slice_gpu_vram_by_node(
    slices: list[dict[str, Any]], node_names: set[str] | None = None
) -> dict[str, int]:
    by_node: dict[str, int] = {}
    for item in slices:
        spec = item.get("spec") or {}
        if spec.get("driver") not in _gpu_device_classes():
            continue
        node_name = str(spec.get("nodeName") or "").strip()
        if node_names and node_name not in node_names:
            continue
        for device in spec.get("devices") or []:
            capacity = device.get("capacity") or {}
            memory = capacity.get("memory")
            if isinstance(memory, dict):
                memory = memory.get("value")
            by_node[node_name] = by_node.get(node_name, 0) + _memory_bytes(memory)
    return by_node


def _resource_slice_gpu_vram_bytes(slices: list[dict[str, Any]], node_names: set[str] | None = None) -> int:
    return sum(_resource_slice_gpu_vram_by_node(slices, node_names).values())


async def _node_disk_usage(cluster_id: str, node_names: set[str]) -> dict[str, dict[str, int]]:
    """Query each node's own kubelet directly (``GET .../nodes/<name>/proxy/
    stats/summary``) for its root filesystem (``node.fs``) capacity/used
    bytes -- independent of whichever in-cluster Prometheus/monitoring stack
    happens to be installed on *this* cluster.

    This deliberately bypasses that cluster's own monitoring: if its disk is
    what's actually full, Prometheus may itself be unable to scrape/ingest/
    even evaluate its own "disk almost full" alerting rules (WAL writes fail
    with ENOSPC), so relying on it to report "the disk is full" is a
    chicken-and-egg problem. The kubelet Summary API is served by the
    kubelet process itself (proxied through the apiserver, so only a working
    kubeconfig is needed) and keeps answering regardless of what else on the
    node has stopped working.
    """
    if not node_names:
        return {}

    async def _fetch(name: str) -> tuple[str, dict[str, int] | None]:
        try:
            result = await run_kubectl(
                ["get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary"],
                cluster_id=cluster_id,
                timeout=10,
            )
        except (FileNotFoundError, TimeoutError):
            return name, None
        if result.returncode != 0:
            return name, None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return name, None
        fs = ((payload.get("node") or {}).get("fs")) or {}
        capacity = fs.get("capacityBytes")
        used = fs.get("usedBytes")
        if capacity is None or used is None:
            return name, None
        return name, {"capacityBytes": _int(capacity), "usedBytes": _int(used)}

    results = await asyncio.gather(*(_fetch(name) for name in node_names))
    return {name: stats for name, stats in results if stats is not None}


def _node_usage_summary(top_nodes: list[dict[str, Any]], node_names: set[str]) -> dict[str, float | None]:
    by_node = _node_usage_by_node(top_nodes, node_names)
    if not by_node:
        return {"cpuMillicores": None, "memoryBytes": None}
    return {
        "cpuMillicores": sum(item["cpuMillicores"] for item in by_node.values()),
        "memoryBytes": sum(item["memoryBytes"] for item in by_node.values()),
    }


def _node_usage_by_node(top_nodes: list[dict[str, Any]], node_names: set[str]) -> dict[str, dict[str, int]]:
    by_node: dict[str, dict[str, int]] = {}
    for item in top_nodes:
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "")
        if node_names and name not in node_names:
            continue
        usage = item.get("usage") or {}
        if name:
            by_node[name] = {
                "cpuMillicores": _cpu_millicores(usage.get("cpu")),
                "memoryBytes": _memory_bytes(usage.get("memory")),
            }
    return by_node


def _usage_percent(used: float | None, total: float | None) -> float | None:
    if used is None or not total or total <= 0:
        return None
    return round(min(100.0, max(0.0, float(used) / float(total) * 100)), 1)


def _device_sort_key(dev_id: str) -> tuple[int, str]:
    cleaned = str(dev_id).strip().lower()
    if cleaned.isdigit():
        return (0, f"{int(cleaned):05d}")
    if cleaned.startswith("gpu-") and cleaned[4:].isdigit():
        return (0, f"{int(cleaned[4:]):05d}")
    if cleaned.startswith("gpu") and cleaned[3:].isdigit():
        return (0, f"{int(cleaned[3:]):05d}")
    return (1, cleaned)


def _build_node_gpu_list(
    node_name: str,
    gpu_count: int,
    gpu_usage_percent: float | None,
    vram_total_bytes: int,
    vram_usage_percent: float | None,
    per_device_metrics: dict[tuple[str, str], dict[str, Any]],
    gpu_devices: dict[tuple[str, str], dict[str, str]],
) -> list[dict[str, Any]]:
    node_device_keys = [key for key in per_device_metrics if key[0] == node_name]
    if not node_device_keys:
        node_device_keys = [key for key, dev in gpu_devices.items() if dev.get("node") == node_name]

    node_device_keys.sort(key=lambda key: _device_sort_key(key[1]))

    effective_count = max(gpu_count, len(node_device_keys))
    if effective_count <= 0:
        return []

    gpus = []
    vram_per_gpu = int(vram_total_bytes / effective_count) if (vram_total_bytes and effective_count > 0) else 0

    for i in range(effective_count):
        key = node_device_keys[i] if i < len(node_device_keys) else None
        dev_meta = gpu_devices.get(key, {}) if key else {}
        dev_metrics = per_device_metrics.get(key, {}) if key else {}

        dev_pci = dev_meta.get("pciAddress") or (key[1] if key else f"0000:0{i + 3}:00.0")

        compute_usage = dev_metrics.get("computeUsagePercent")
        if compute_usage is None:
            compute_usage = gpu_usage_percent if gpu_usage_percent is not None else 0.0
        else:
            compute_usage = round(min(100.0, max(0.0, float(compute_usage))), 1)

        dev_vram_total = int(dev_metrics.get("vramTotalBytes") or vram_per_gpu)
        dev_vram_used = dev_metrics.get("vramUsedBytes")
        if dev_vram_used is None:
            if vram_usage_percent is not None and dev_vram_total > 0:
                dev_vram_used = int(dev_vram_total * (vram_usage_percent / 100.0))
            else:
                dev_vram_used = 0
        else:
            dev_vram_used = int(dev_vram_used)

        dev_vram_usage_pct = _usage_percent(dev_vram_used, dev_vram_total) or 0.0

        read_bytes = dev_metrics.get("vramReadThroughputBytes")
        write_bytes = dev_metrics.get("vramWriteThroughputBytes")
        read_bytes = int(read_bytes) if read_bytes is not None else 0
        write_bytes = int(write_bytes) if write_bytes is not None else 0

        total_bw_bytes = read_bytes + write_bytes

        bw_pct = dev_metrics.get("vramBandwidthPercent")
        if bw_pct is None:
            if total_bw_bytes > 0:
                max_bw = 3.2 * (1024**4)
                bw_pct = round(min(100.0, max(0.0, (total_bw_bytes / max_bw) * 100)), 1)
            else:
                bw_pct = 0.0
        else:
            bw_pct = round(min(100.0, max(0.0, float(bw_pct))), 1)

        gpus.append(
            {
                "id": f"gpu-{i}",
                "index": i,
                "name": f"GPU #{i}",
                "pciAddress": dev_pci,
                "computeUsagePercent": compute_usage,
                "vramTotalBytes": dev_vram_total,
                "vramUsedBytes": dev_vram_used,
                "vramUsagePercent": dev_vram_usage_pct,
                "vramReadThroughputBytes": read_bytes,
                "vramWriteThroughputBytes": write_bytes,
                "vramTotalThroughputBytes": total_bw_bytes,
                "vramBandwidthPercent": bw_pct,
            }
        )

    return gpus


def _annotate_node_usage(
    node_summaries: list[dict[str, Any]],
    node_usage: dict[str, dict[str, int]],
    prometheus_metrics: dict[str, Any],
    fallback_vram_by_node: dict[str, int] | None = None,
    gpu_devices: dict[tuple[str, str], dict[str, str]] | None = None,
    disk_usage_by_node: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    node_vram = prometheus_metrics.get("nodeVram") or {}
    node_gpu = prometheus_metrics.get("nodeGpu") or {}
    node_cpu = prometheus_metrics.get("nodeCpu") or {}
    node_memory = prometheus_metrics.get("nodeMemory") or {}
    per_device_metrics = prometheus_metrics.get("perDeviceMetrics") or {}
    fallback_vram_by_node = fallback_vram_by_node or {}
    gpu_devices = gpu_devices or {}
    disk_usage_by_node = disk_usage_by_node or {}
    annotated = []
    for node in node_summaries:
        node_name = str(node.get("name") or "")
        usage = node_usage.get(node_name) or {}
        vram = node_vram.get(node_name) or {}
        gpu_usage = node_gpu.get(node_name)
        vram_total = int(vram.get("totalBytes") or fallback_vram_by_node.get(node_name) or 0)
        gpu_count = node.get("gpuCount") or (1 if node.get("gpu") else 0)
        gpu_usage_pct = round(min(100.0, max(0.0, float(gpu_usage))), 1) if gpu_usage is not None else None
        vram_usage_pct = _usage_percent(vram.get("usedBytes"), vram_total)
        disk = disk_usage_by_node.get(node_name) or {}
        disk_total = int(disk.get("capacityBytes") or 0)
        disk_used = disk.get("usedBytes")
        disk_usage_pct = _usage_percent(disk_used, disk_total)

        gpus = _build_node_gpu_list(
            node_name=node_name,
            gpu_count=gpu_count,
            gpu_usage_percent=gpu_usage_pct,
            vram_total_bytes=vram_total,
            vram_usage_percent=vram_usage_pct,
            per_device_metrics=per_device_metrics,
            gpu_devices=gpu_devices,
        )

        annotated.append(
            {
                **node,
                "cpuUsagePercent": node_cpu.get(node_name)
                if node_cpu.get(node_name) is not None
                else _usage_percent(usage.get("cpuMillicores"), (node.get("cpu") or 0) * 1000),
                "memoryUsagePercent": node_memory.get(node_name)
                if node_memory.get(node_name) is not None
                else _usage_percent(usage.get("memoryBytes"), node.get("memoryBytes") or 0),
                "gpuUsagePercent": gpu_usage_pct,
                "vramBytes": vram_total,
                "vramUsagePercent": vram_usage_pct,
                "diskBytes": disk_total,
                "diskUsedBytes": int(disk_used) if disk_used is not None else None,
                "diskUsagePercent": disk_usage_pct,
                "gpus": gpus,
            }
        )
    return annotated


def _active_gpu_device_keys(resource_claims: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Return non-admin Intel DRA devices reserved by active claims."""
    allocated: set[tuple[str, str]] = set()
    for claim in resource_claims:
        results = (((claim.get("status") or {}).get("allocation") or {}).get("devices") or {}).get("results") or []
        for result in results:
            if (
                any(device_class in str(result.get("driver") or "").lower() for device_class in _gpu_device_classes())
                and not result.get("adminAccess", False)
            ):
                pool = str(result.get("pool") or "").strip()
                device = str(result.get("device") or "").strip()
                if pool and device:
                    allocated.add((pool, device))
    return allocated


def _node_summary(
    node: dict[str, Any],
    resource_slice_counts: dict[str, int] | None = None,
    resource_slice_profile_counts: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    metadata = node.get("metadata") or {}
    name = str(metadata.get("name") or "unknown")
    labels = metadata.get("labels") or {}
    is_control_plane = "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels
    # A control-plane node is treated as a worker too when it is not tainted
    # with NoSchedule, because workloads (incl. GPU jobs) still schedule on it.
    # This covers single-node/kind clusters where the control plane doubles as
    # the sole worker.
    taints = (node.get("spec") or {}).get("taints") or []
    control_plane_no_schedule = any(
        taint.get("key") in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
        and taint.get("effect") == "NoSchedule"
        for taint in taints
    )
    role = "control-plane" if (is_control_plane and control_plane_no_schedule) else "worker"
    status = node.get("status") or {}
    node_info = status.get("nodeInfo") or {}
    conditions = status.get("conditions") or []
    capacity = status.get("capacity") or {}
    gpu_label = _node_has_gpu_label(labels)
    # GPUs may be exposed as extended resources (plugin mode) or via DRA
    # ResourceSlices; take the larger so neither access mode is double-counted.
    gpu_count = max(_gpu_count(capacity), (resource_slice_counts or {}).get(name, 0))
    gpu_by_profile = _gpu_counts_by_profile(capacity)
    for profile_id, count in (resource_slice_profile_counts or {}).get(name, {}).items():
        gpu_by_profile[profile_id] = max(gpu_by_profile.get(profile_id, 0), count)
    return {
        "name": name,
        "role": role,
        "version": str(node_info.get("kubeletVersion") or "unknown"),
        "osImage": str(node_info.get("osImage") or "unknown"),
        "ready": any(
            condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions
        ),
        # `spec.unschedulable` is the field `kubectl cordon`/`kubectl uncordon`
        # toggle; surfaced so the UI can show a "Maintenance" state distinct
        # from Ready/Not Ready.
        "schedulingDisabled": bool((node.get("spec") or {}).get("unschedulable", False)),
        "cpu": _cpu_cores(capacity.get("cpu")),
        "memoryBytes": _memory_bytes(capacity.get("memory")),
        "gpu": gpu_label or gpu_count > 0,
        "gpuCount": gpu_count,
        "gpuByProfile": gpu_by_profile,
        "cachedImages": sorted(
            {
                name
                for image in (status.get("images") or [])
                for name in (image.get("names") or [])
                if isinstance(name, str) and name
            }
        ),
    }


def _active_gpu_allocations(pods: list[dict[str, Any]], resource_claims: list[dict[str, Any]]) -> int:
    """Count Intel GPUs reserved by active pods and exclusive DRA claims."""
    pod_allocations = 0
    for pod in pods:
        if str((pod.get("status") or {}).get("phase") or "") in {"Succeeded", "Failed"}:
            continue
        spec = pod.get("spec") or {}
        for container in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
            resources = container.get("resources") or {}
            requests = resources.get("requests") or resources.get("limits") or {}
            pod_allocations += _gpu_count(requests)

    dra_allocations = 0
    for claim in resource_claims:
        results = (((claim.get("status") or {}).get("allocation") or {}).get("devices") or {}).get("results") or []
        dra_allocations += sum(
            1
            for result in results
            if any(device_class in str(result.get("driver") or "").lower() for device_class in _gpu_device_classes())
            and not result.get("adminAccess", False)
        )
    return pod_allocations + dra_allocations


def _hardware_summary(
    node_summaries: list[dict[str, Any]],
    allocated_gpu_count: int = 0,
    gpu_devices: dict[tuple[str, str], dict[str, str]] | None = None,
    allocated_gpu_devices: set[tuple[str, str]] | None = None,
    vram_bytes: int = 0,
    node_usage: dict[str, float | None] | None = None,
    prometheus_metrics: dict[str, Any] | None = None,
    gpu_by_profile: dict[str, int] | None = None,
    device_metrics: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate per-node capacity into a cluster-level hardware summary."""
    gpu_count = sum(node.get("gpuCount") or 0 for node in node_summaries)
    allocated_gpu_count = min(gpu_count, max(0, allocated_gpu_count))
    available_gpu_count = max(0, gpu_count - allocated_gpu_count)
    configured_devices = [
        address.strip() for address in os.environ.get("PRISM_GPU_PCI_ALLOWLIST", "").split(",") if address.strip()
    ]
    configured_device_set = set(configured_devices)
    target_node = os.environ.get("PRISM_K8S_TARGET_NODE", "").strip()
    configured_gpu_limit = len(configured_device_set) or None
    usable_gpu_count = min(available_gpu_count, configured_gpu_limit) if configured_gpu_limit else available_gpu_count
    if gpu_devices:
        eligible_devices = {
            key
            for key, device in gpu_devices.items()
            if (not target_node or device["node"] == target_node)
            and (not configured_device_set or device["pciAddress"] in configured_device_set)
        }
        if target_node or configured_device_set:
            configured_gpu_limit = len(eligible_devices)
        usable_gpu_count = min(
            available_gpu_count,
            len(eligible_devices - (allocated_gpu_devices or set())),
        )
    cpu_cores = round(sum(node.get("cpu") or 0.0 for node in node_summaries), 2)
    memory_bytes = sum(node.get("memoryBytes") or 0 for node in node_summaries)
    cpu_usage_percent = None
    memory_usage_percent = None
    if node_usage:
        used_millicores = node_usage.get("cpuMillicores")
        used_memory_bytes = node_usage.get("memoryBytes")
        if used_millicores is not None and cpu_cores > 0:
            cpu_usage_percent = round(min(100.0, max(0.0, float(used_millicores) / (cpu_cores * 1000) * 100)), 1)
        if used_memory_bytes is not None and memory_bytes > 0:
            memory_usage_percent = round(min(100.0, max(0.0, float(used_memory_bytes) / memory_bytes * 100)), 1)
    prometheus_metrics = prometheus_metrics or {}
    if prometheus_metrics.get("cpuUsagePercent") is not None:
        cpu_usage_percent = round(min(100.0, max(0.0, float(prometheus_metrics["cpuUsagePercent"]))), 1)
    if prometheus_metrics.get("memoryUsagePercent") is not None:
        memory_usage_percent = round(min(100.0, max(0.0, float(prometheus_metrics["memoryUsagePercent"]))), 1)
    gpu_usage_percent = prometheus_metrics.get("gpuUsagePercent")
    if gpu_usage_percent is None and gpu_count > 0:
        gpu_usage_percent = allocated_gpu_count / gpu_count * 100
    vram_bytes = int(prometheus_metrics.get("vramBytes") or vram_bytes or 0)
    vram_usage_percent = prometheus_metrics.get("vramUsagePercent")
    # Disk capacity/usage comes from each node's own kubelet Summary API (see
    # `_node_disk_usage`), already annotated onto `node_summaries` -- summed/
    # weighted here the same way CPU and memory are, but deliberately *not*
    # sourced from this cluster's own Prometheus (see that function's
    # docstring for why).
    disk_bytes = sum(node.get("diskBytes") or 0 for node in node_summaries)
    disk_used_bytes = sum(
        node.get("diskUsedBytes") or 0 for node in node_summaries if node.get("diskUsedBytes") is not None
    )
    nodes_reporting_disk = sum(1 for node in node_summaries if node.get("diskUsedBytes") is not None)
    disk_usage_percent = (
        round(min(100.0, max(0.0, disk_used_bytes / disk_bytes * 100)), 1)
        if disk_bytes and nodes_reporting_disk
        else None
    )
    return {
        "nodes": len(node_summaries),
        "cpuCores": cpu_cores,
        "cpuUsagePercent": cpu_usage_percent,
        "memoryBytes": memory_bytes,
        "memoryUsagePercent": memory_usage_percent,
        "gpuNodes": sum(1 for node in node_summaries if node.get("gpu")),
        "gpuCount": gpu_count,
        "totalGpuCount": gpu_count,
        "accelerators": [
            {"id": profile_id, "gpuCount": count}
            for profile_id, count in sorted((gpu_by_profile or {}).items())
        ],
        "deviceMetrics": list(device_metrics or []),
        "gpuUsagePercent": round(min(100.0, max(0.0, gpu_usage_percent)), 1) if gpu_usage_percent is not None else None,
        "vramBytes": vram_bytes,
        "vramUsagePercent": round(min(100.0, max(0.0, vram_usage_percent)), 1)
        if vram_usage_percent is not None
        else None,
        "diskBytes": disk_bytes,
        "diskUsagePercent": disk_usage_percent,
        "allocatedGpuCount": allocated_gpu_count,
        "availableGpuCount": available_gpu_count,
        "usableGpuCount": usable_gpu_count,
        "configuredGpuLimit": configured_gpu_limit,
    }


async def build_overview(cluster: registry.Cluster) -> dict[str, Any]:
    kubeconfig = registry.kubeconfig_path(cluster.id)
    if not kubeconfig.is_file():
        raise ClusterOverviewError(
            "Cluster kubeconfig is not readable", status_code=409, code="cluster_kubeconfig_missing"
        )
    (
        nodes_result,
        namespaces_result,
        components_result,
        pods_result,
        claims_result,
        slices_result,
        top_nodes_result,
        monitoring,
    ) = await asyncio.gather(
        run_kubectl(["get", "nodes", "-o", "json"], cluster_id=cluster.id, timeout=30),
        run_kubectl(["get", "namespaces", "-o", "json"], cluster_id=cluster.id, timeout=30),
        run_kubectl(
            ["get", "daemonsets,deployments,statefulsets", "-A", "-o", "json"], cluster_id=cluster.id, timeout=30
        ),
        run_kubectl(
            ["get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed", "-o", "json"],
            cluster_id=cluster.id,
            timeout=30,
        ),
        run_kubectl(["get", "resourceclaims.resource.k8s.io", "-A", "-o", "json"], cluster_id=cluster.id, timeout=30),
        run_kubectl(["get", "resourceslices.resource.k8s.io", "-o", "json"], cluster_id=cluster.id, timeout=30),
        # metrics-server is optional and often absent/slow; a bounded probe keeps
        # it from holding the whole overview (and the Configure-deployment cluster
        # field) for the full timeout when it is not installed.
        run_kubectl(["get", "--raw=/apis/metrics.k8s.io/v1beta1/nodes"], cluster_id=cluster.id, timeout=5),
        monitoring_component(cluster),
    )
    nodes = json_output(nodes_result, "kubectl get nodes").get("items") or []
    namespaces = [
        item
        for item in (json_output(namespaces_result, "kubectl get namespaces").get("items") or [])
        if str((item.get("metadata") or {}).get("name") or "").startswith(cluster_settings.namespace_prefix)
    ]
    raw_components = json_output(components_result, "kubectl get cluster components").get("items") or []
    deployments = [item for item in raw_components if (item.get("kind") or "").lower() == "deployment"]
    components = component_status(raw_components)
    components["monitoring"] = monitoring
    pods = json_output(pods_result, "kubectl get pods").get("items") or []
    resource_claims = (
        json_output(claims_result, "kubectl get resource claims").get("items") or []
        if claims_result.returncode == 0
        else []
    )
    # ResourceSlice CRD is optional; treat "not found" as no DRA slices.
    resource_slice_counts: dict[str, int] = {}
    resource_slice_profile_counts: dict[str, dict[str, int]] = {}
    gpu_devices: dict[tuple[str, str], dict[str, str]] = {}
    resource_slices: list[dict[str, Any]] = []
    if slices_result.returncode == 0:
        resource_slices = json_output(slices_result, "kubectl get resource slices").get("items") or []
        gpu_devices = _resource_slice_gpu_devices(resource_slices)
        resource_slice_counts = _resource_slice_gpu_counts(resource_slices)
        resource_slice_profile_counts = _resource_slice_gpu_counts_by_profile(resource_slices)
    node_summaries = [
        summary
        for summary in (
            _node_summary(node, resource_slice_counts, resource_slice_profile_counts) for node in nodes
        )
        if summary.get("role") == "worker"
    ]
    worker_node_names = {node["name"] for node in node_summaries}
    vram_by_node = _resource_slice_gpu_vram_by_node(resource_slices, worker_node_names)
    vram_bytes = sum(vram_by_node.values())
    top_nodes = (
        json_output(top_nodes_result, "kubectl node metrics").get("items") or []
        if top_nodes_result.returncode == 0
        else []
    )
    allocated_gpu_count = _active_gpu_allocations(pods, resource_claims)
    prometheus_metrics, disk_usage_by_node = await asyncio.gather(
        _prometheus_hardware_metrics(cluster.id, worker_node_names, gpu_devices),
        _node_disk_usage(cluster.id, worker_node_names),
    )
    node_usage_by_node = _node_usage_by_node(top_nodes, worker_node_names)
    node_usage = _node_usage_summary(top_nodes, worker_node_names)
    node_summaries = _annotate_node_usage(
        node_summaries,
        node_usage_by_node,
        prometheus_metrics,
        vram_by_node,
        gpu_devices,
        disk_usage_by_node,
    )
    gpu_by_profile: dict[str, int] = {}
    for summary in node_summaries:
        for profile_id, count in (summary.get("gpuByProfile") or {}).items():
            gpu_by_profile[profile_id] = gpu_by_profile.get(profile_id, 0) + int(count)
    device_metrics = _available_device_metrics(set(gpu_by_profile))
    return {
        "components": components,
        "nodes": node_summaries,
        "namespaces": namespace_summary(namespaces, deployments),
        "hardware": _hardware_summary(
            node_summaries,
            allocated_gpu_count,
            gpu_devices,
            _active_gpu_device_keys(resource_claims),
            vram_bytes=vram_bytes,
            node_usage=node_usage,
            prometheus_metrics=prometheus_metrics,
            gpu_by_profile=gpu_by_profile,
            device_metrics=device_metrics,
        ),
    }
