"""Join XPUM device history to Intel DRA allocations before aggregating it.

XPUM's namespace/pod labels describe the exporter, not the consumer. Never
attribute every GPU on a model server's node to that model server.
"""

import math
import re
from collections import defaultdict
from datetime import datetime

XPUM_SOURCE = "Intel XPUM device telemetry (DRA allocation)"

# Queries and metric labels come from the Intel hardware profile
# (telemetry.device_metrics / label_schema). The literals below are the
# compatibility fallback for when hardware discovery is unavailable; for the
# built-in Intel profile they are identical, so behavior is unchanged.
_FALLBACK_QUERIES = {
    "gpu_utilization_percent": 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}',
    "gpu_framebuffer_used_bytes": 'hw_memory_usage_bytes{hw_memory_location="device"}',
}
_FALLBACK_LABELS = {"node": "node", "pci": "pci_bdf", "device": "com_intel_subdevice_id"}
_QUERY_METRIC_KEYS = {
    "gpu_utilization_percent": "utilization",
    "gpu_framebuffer_used_bytes": "framebuffer_used",
}


def _intel_profile():
    """Intel XPU hardware profile, or None when discovery is unavailable."""
    try:
        from llm_d_bench.hardware.resolver import resolve_by_accelerator_key

        return resolve_by_accelerator_key("intel_gpu")
    except Exception:  # pragma: no cover - profiling must not fail on discovery errors
        return None


def _telemetry():
    profile = _intel_profile()
    return profile.telemetry if profile is not None else None


def _label_schema() -> dict[str, str]:
    telemetry = _telemetry()
    if telemetry is not None and telemetry.label_schema:
        return {**_FALLBACK_LABELS, **dict(telemetry.label_schema)}
    return dict(_FALLBACK_LABELS)


def xpum_queries() -> dict[str, str]:
    """PromQL for each XPUM metric, from the profile with a literal fallback."""
    telemetry = _telemetry()
    metrics = dict(telemetry.device_metrics) if telemetry is not None else {}
    return {key: metrics.get(metric_key) or _FALLBACK_QUERIES[key] for key, metric_key in _QUERY_METRIC_KEYS.items()}


XPUM_QUERIES = xpum_queries()
XPUM_LABELS = _label_schema()


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def device_allocations(pods, claims, slices, namespace):
    """Return exclusive (node, PCI) -> consumer and allocation start mappings."""
    devices = {}
    for item in slices:
        spec = item.get("spec") or {}
        if spec.get("driver") != "gpu.intel.com":
            continue
        node = spec.get("nodeName")
        pool = (spec.get("pool") or {}).get("name")
        for device in spec.get("devices") or []:
            attrs = device.get("attributes") or {}
            pci = (attrs.get("pciAddress") or attrs.get("resource.kubernetes.io/pciBusID") or {}).get("string")
            if node and pool and pci:
                devices[(pool, device.get("name"))] = (node, pci.lower())
    by_uid = {
        p.get("metadata", {}).get("uid"): p
        for p in pods
        if p.get("metadata", {}).get("uid") and p.get("metadata", {}).get("namespace") == namespace
    }
    owners = defaultdict(list)
    for claim in claims:
        if claim.get("metadata", {}).get("namespace") != namespace:
            continue
        status = claim.get("status") or {}
        consumers = status.get("reservedFor") or []
        # Shared/admin allocations are not exclusive Pod utilization evidence.
        if len(consumers) != 1 or consumers[0].get("resource") != "pods":
            continue
        pod = by_uid.get(consumers[0].get("uid"))
        if not pod or pod.get("metadata", {}).get("name") != consumers[0].get("name"):
            continue
        allocation = status.get("allocation") or {}
        since = max(
            _timestamp(allocation.get("allocationTimestamp")),
            _timestamp(pod.get("status", {}).get("startTime")),
            _timestamp(pod.get("metadata", {}).get("creationTimestamp")),
        )
        for result in allocation.get("devices", {}).get("results") or []:
            if result.get("driver") != "gpu.intel.com" or result.get("adminAccess") or result.get("shareID"):
                continue
            identity = devices.get((result.get("pool"), result.get("device")))
            # Intel DRA encodes PCI BDF in its device ID. A driver refresh can
            # temporarily remove ResourceSlices while allocated claims survive.
            if identity is None and result.get("pool") == pod.get("spec", {}).get("nodeName"):
                pci_id = re.fullmatch(
                    r"([0-9a-f]{4})-([0-9a-f]{2})-([0-9a-f]{2})-([0-7])-0x[0-9a-f]{4}",
                    str(result.get("device") or "").lower(),
                )
                if pci_id:
                    domain, bus, slot, function = pci_id.groups()
                    identity = (result["pool"], f"{domain}:{bus}:{slot}.{function}")
            if identity and identity[0] == pod.get("spec", {}).get("nodeName"):
                owners[identity].append({"pod": consumers[0]["name"], "since": since})
    return {
        identity: entries[0] for identity, entries in owners.items() if len({entry["pod"] for entry in entries}) == 1
    }


def aggregate_xpum(samples, allocations, metric):
    """Produce normal Prometheus matrices for the deployment and each Pod.

    Require node+PCI identity. Prefer whole-device samples to tiles; deduplicate
    exporter replicas before summing bytes or averaging device utilization.
    """
    cells = defaultdict(dict)
    utilization = metric == "gpu_utilization_percent"
    for sample in samples:
        labels = sample.get("metric") or {}
        node = (labels.get(XPUM_LABELS["node"]) or labels.get("nodename")
                or labels.get("hostname") or labels.get("k8s_node_name"))
        identity = (node, str(labels.get(XPUM_LABELS["pci"]) or "").lower())
        allocation = allocations.get(identity)
        if not allocation:
            continue
        if utilization and labels.get("hw_gpu_task", "compute-all") != "compute-all":
            continue
        if not utilization and labels.get("hw_memory_location", "device") != "device":
            continue
        tile = labels.get(XPUM_LABELS["device"]) or ""
        for timestamp, raw in sample.get("values") or []:
            try:
                timestamp, value = float(timestamp), float(raw)
            except (ValueError, TypeError):
                continue
            if not math.isfinite(value) or value < 0 or timestamp < allocation["since"]:
                continue
            if utilization and value > 1:
                continue
            key = (identity, timestamp)
            # Duplicate scrapes are one device sample, never extra devices.
            cells[key][tile] = max(cells[key].get(tile, value), value)
    deployment = defaultdict(list)
    pods = defaultdict(lambda: defaultdict(list))
    for (identity, timestamp), tiles in cells.items():
        value = tiles[""] if "" in tiles else sum(tiles.values()) / len(tiles) if utilization else sum(tiles.values())
        value *= 100 if utilization else 1
        deployment[timestamp].append(value)
        pods[allocations[identity]["pod"]][timestamp].append(value)

    def matrix(points, labels):
        return {
            "metric": labels,
            "values": [
                [timestamp, sum(values) / len(values) if utilization else sum(values)]
                for timestamp, values in sorted(points.items())
            ],
        }

    return (
        [matrix(deployment, {})] if deployment else [],
        [matrix(points, {"pod": pod}) for pod, points in sorted(pods.items())],
    )
