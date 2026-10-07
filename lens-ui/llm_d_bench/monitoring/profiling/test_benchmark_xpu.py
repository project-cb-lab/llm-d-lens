"""XPUM history must describe allocated devices, not the exporter Pod."""

import pytest

from llm_d_bench.monitoring.profiling import service


@pytest.fixture
def inventory():
    pods = [
        {"metadata": {"name": name, "namespace": "bench", "uid": name}, "spec": {"nodeName": "node-a"}, "status": {}}
        for name in ("prefill-0", "decode-0")
    ]
    claims = [
        {
            "metadata": {"namespace": "bench"},
            "status": {
                "reservedFor": [{"resource": "pods", "name": pod, "uid": pod}],
                "allocation": {
                    "devices": {
                        "results": [{"driver": "gpu.intel.com", "pool": "node-a", "device": dev} for dev in devices]
                    }
                },
            },
        }
        for pod, devices in (("prefill-0", ["gpu0", "gpu1"]), ("decode-0", ["gpu2"]))
    ]
    slices = [
        {
            "spec": {
                "driver": "gpu.intel.com",
                "nodeName": node,
                "pool": {"name": node},
                "devices": [
                    {"name": f"gpu{i}", "attributes": {"pciAddress": {"string": f"0000:0{i}:00.0"}}} for i in range(4)
                ],
            }
        }
        for node in ("node-a", "node-b")
    ]
    return {"pods": pods, "resourceclaims.resource.k8s.io": claims, "resourceslices.resource.k8s.io": slices}


def sample(i, values, **labels):
    return {
        "metric": {
            "node": "node-a",
            "pci_bdf": f"0000:0{i}:00.0",
            "namespace": "intel-xpumd",
            "pod": "xpumd-exporter",
            **labels,
        },
        "values": [[1000 + j * 5, str(value)] for j, value in enumerate(values)],
    }


async def collect(monkeypatch, inventory, utilization, memory):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench", "cluster"))

    async def port(*_):
        return 19090

    async def resources(kind, **_):
        return inventory.get(kind, [])

    async def components(*_):
        return {"prefill": ["prefill-0"], "decode": ["decode-0"], "epp": []}

    async def query(_client, promql, *_):
        if "hw_gpu_utilization_ratio" in promql:
            assert 'hw_gpu_task="compute-all"' in promql
            return utilization
        if "hw_memory_usage_bytes" in promql:
            assert 'hw_memory_location="device"' in promql
            return memory
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", port)
    monkeypatch.setattr(service, "list_resources", resources)
    monkeypatch.setattr(service, "_discover_components", components)
    monkeypatch.setattr(service, "_query_range", query)
    return await service.collect_benchmark_observability("execution", "1970-01-01T00:16:40Z", "1970-01-01T00:17:00Z")


@pytest.mark.asyncio
async def test_xpum_history_is_scoped_to_allocated_devices_and_pods(monkeypatch, inventory):
    # Three allocated devices: mean (0 + 50 + 100) / 3 = 50%; bytes sum = 600.
    util = [
        sample(0, [0, 0]),
        sample(1, [0.5, 0.5]),
        sample(2, [1, 1]),
        sample(3, [1, 1]),
        sample(0, [1, 1], node="node-b"),
        sample(1, [0.5, 0.5], job="duplicate-scrape"),
    ]
    memory = [sample(i, [value, value]) for i, value in enumerate([100, 200, 300, 9999])]
    result = await collect(monkeypatch, inventory, util, memory)
    assert result["series"][0]["gpu_utilization_percent"] == 50
    assert result["summary"]["gpu_framebuffer_used_bytes"]["mean"] == 600
    pods = {p["pod"]: p for p in result["per_pod"]}
    assert set(pods) == {"prefill-0", "decode-0"}
    assert pods["prefill-0"]["gpu_utilization_percent"]["mean"] == 25
    assert pods["decode-0"]["gpu_framebuffer_used_bytes"]["mean"] == 300
    assert "XPUM" in result["evidence"]["gpu_xpu_utilization"]["source"]
    assert result["availability"]["metrics"]["gpu_utilization_percent"] == "available"


@pytest.mark.asyncio
async def test_unassigned_or_ambiguous_devices_are_not_benchmark_utilization(monkeypatch, inventory):
    inventory["resourceclaims.resource.k8s.io"] = []
    result = await collect(monkeypatch, inventory, [sample(0, [0.8])], [sample(0, [100])])
    assert "gpu_utilization_percent" not in result["summary"]
    assert result["evidence"]["gpu_xpu_utilization"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_missing_nonfinite_and_idle_xpum_are_distinct(monkeypatch, inventory):
    result = await collect(monkeypatch, inventory, [sample(0, [0, "NaN"])], [])
    assert result["summary"]["gpu_utilization_percent"]["mean"] == 0
    assert "gpu_framebuffer_used_bytes" not in result["summary"]
    assert result["availability"]["metrics"]["gpu_framebuffer_used_bytes"] == "unavailable"


@pytest.mark.asyncio
async def test_history_before_allocation_is_excluded(monkeypatch, inventory):
    for claim in inventory["resourceclaims.resource.k8s.io"]:
        claim["status"]["allocation"]["allocationTimestamp"] = "1970-01-01T00:16:45Z"
    result = await collect(monkeypatch, inventory, [sample(0, [0.9, 0.2])], [])
    assert result["summary"]["gpu_utilization_percent"]["mean"] == 20
    assert len(result["series"]) == 1


@pytest.mark.asyncio
async def test_tiles_do_not_double_count_whole_device_and_missing_nodes_are_unsafe(monkeypatch, inventory):
    util = [
        sample(0, [0.4]),
        sample(0, [0.9], com_intel_subdevice_id="0"),
        sample(1, [0.2], com_intel_subdevice_id="0"),
        sample(1, [0.6], com_intel_subdevice_id="1"),
        sample(2, [1], node=""),
    ]
    memory = [
        sample(0, [100]),
        sample(0, [90], com_intel_subdevice_id="0"),
        sample(1, [20], com_intel_subdevice_id="0"),
        sample(1, [30], com_intel_subdevice_id="1"),
    ]
    result = await collect(monkeypatch, inventory, util, memory)
    assert result["summary"]["gpu_utilization_percent"]["mean"] == 40
    assert result["summary"]["gpu_framebuffer_used_bytes"]["mean"] == 150


@pytest.mark.asyncio
async def test_shared_claim_and_stale_pod_uid_are_excluded(monkeypatch, inventory):
    claims = inventory["resourceclaims.resource.k8s.io"]
    claims[0]["status"]["reservedFor"].append({"resource": "pods", "name": "other", "uid": "other"})
    claims[1]["status"]["reservedFor"][0]["uid"] = "old-pod-uid"
    result = await collect(monkeypatch, inventory, [sample(0, [0.8]), sample(2, [0.8])], [])
    assert "gpu_utilization_percent" not in result["summary"]


def test_xpum_queries_and_labels_come_from_intel_profile():
    from llm_d_bench.hardware.registry import get_profile
    from llm_d_bench.monitoring.profiling import xpu_metrics

    telemetry = get_profile("intel-xpu").telemetry
    assert telemetry is not None
    queries = xpu_metrics.xpum_queries()
    assert queries["gpu_utilization_percent"] == telemetry.device_metrics["utilization"]
    assert queries["gpu_framebuffer_used_bytes"] == telemetry.device_metrics["framebuffer_used"]
    assert xpu_metrics.XPUM_LABELS["pci"] == telemetry.label_schema["pci"]
    assert xpu_metrics.XPUM_LABELS["device"] == telemetry.label_schema["device"]


@pytest.mark.asyncio
async def test_intel_pci_device_ids_survive_temporary_resourceslice_unavailability(monkeypatch, inventory):
    inventory["resourceslices.resource.k8s.io"] = []
    for claim in inventory["resourceclaims.resource.k8s.io"]:
        for result in claim["status"]["allocation"]["devices"]["results"]:
            result["device"] = f"0000-0{result['device'][-1]}-00-0-0xe211"
    result = await collect(monkeypatch, inventory, [sample(0, [0.4])], [sample(0, [100])])
    assert result["summary"]["gpu_utilization_percent"]["mean"] == 40
    assert result["summary"]["gpu_framebuffer_used_bytes"]["mean"] == 100
