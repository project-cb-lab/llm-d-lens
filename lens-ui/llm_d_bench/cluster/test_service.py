"""Tests for cluster hardware capacity accounting."""

import json
from types import SimpleNamespace

import pytest

from llm_d_bench.cluster import registry, service
from llm_d_bench.cluster.errors import ClusterOverviewError
from llm_d_bench.cluster.service import _active_gpu_allocations
from llm_d_bench.cluster.settings import ClusterSettings
from llm_d_bench.utils.shell import CommandResult


def _claim(*results: dict) -> dict:
    return {"status": {"allocation": {"devices": {"results": list(results)}}}}


@pytest.mark.asyncio
async def test_delete_cluster_rejects_when_storage_volumes_registered(monkeypatch):
    """A cluster must not be deletable while it still has Storage volumes
    registered against it -- reproduced live: deleting a cluster first, then
    trying to delete one of its ``local-disk`` volumes, raised
    ``FileNotFoundError`` (the volume's ``cluster_id`` no longer resolved to
    a kubeconfig) and surfaced as a 500 with no way to recover the volume.
    """
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr(
        "llm_d_bench.storage.store.default_store",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(cluster_id="cluster-1")]),
    )

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.delete_cluster("cluster-1")
    assert excinfo.value.code == "cluster_has_storage_volumes"


@pytest.mark.asyncio
async def test_delete_cluster_succeeds_when_no_storage_volumes(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr(
        "llm_d_bench.storage.store.default_store",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(cluster_id="cluster-other")]),
    )
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    deleted_namespaces: list[str] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_bench.utils.kubernetes.delete_namespace", fake_delete_namespace)
    deleted: list[str] = []
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: deleted.append(cluster_id))

    await service.delete_cluster("cluster-1")
    assert deleted == ["cluster-1"]
    assert deleted_namespaces == ["llm-d-bench-storage-cluster-1"]


def test_node_summary_reports_scheduling_disabled_from_spec_unschedulable():
    cordoned = {
        "metadata": {"name": "worker-1", "labels": {}},
        "spec": {"unschedulable": True},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    schedulable = {
        "metadata": {"name": "worker-2", "labels": {}},
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }

    assert service._node_summary(cordoned)["schedulingDisabled"] is True
    assert service._node_summary(schedulable)["schedulingDisabled"] is False


@pytest.mark.asyncio
async def test_set_node_maintenance_cordons_worker_but_rejects_control_plane(monkeypatch):
    nodes_payload = {
        "items": [
            {
                "metadata": {"name": "worker-1", "labels": {}},
                "spec": {"taints": []},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            {
                "metadata": {"name": "cp-1", "labels": {"node-role.kubernetes.io/control-plane": ""}},
                "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
        ]
    }

    calls: list[list[str]] = []

    async def fake_kubectl(command, *_args, **_kwargs):
        if command[:2] == ["get", "nodes"]:
            return CommandResult(("kubectl",), 0, json.dumps(nodes_payload), "")
        calls.append(command)
        return CommandResult(("kubectl",), 0, "node/worker-1 cordoned", "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    results = await service.set_node_maintenance(
        SimpleNamespace(id="cluster"), ["worker-1", "cp-1", "missing-node"], disabled=True
    )

    by_name = {item["name"]: item for item in results}
    assert by_name["worker-1"]["ok"] is True
    assert by_name["worker-1"]["schedulingDisabled"] is True
    assert by_name["cp-1"]["ok"] is False
    assert "control-plane" in by_name["cp-1"]["error"]
    assert by_name["missing-node"]["ok"] is False
    assert calls == [["cordon", "worker-1"]]


def test_active_gpu_allocations_ignore_admin_access_monitoring_claims():
    claims = [
        _claim(
            {"driver": "gpu.intel.com", "device": "gpu-1", "adminAccess": True},
            {"driver": "gpu.intel.com", "device": "gpu-2", "adminAccess": True},
        ),
        _claim({"driver": "gpu.intel.com", "device": "gpu-3"}),
        _claim({"driver": "example.com", "device": "other-device"}),
    ]

    assert _active_gpu_allocations([], claims) == 1


def _slice(node: str, *devices: tuple[str, str]) -> dict:
    return {
        "spec": {
            "driver": "gpu.intel.com",
            "nodeName": node,
            "pool": {"name": node},
            "devices": [{"name": name, "attributes": {"pciAddress": {"string": pci}}} for name, pci in devices],
        }
    }


def _memory_slice(node: str, *memory_values: str) -> dict:
    return {
        "spec": {
            "driver": "gpu.intel.com",
            "nodeName": node,
            "pool": {"name": node},
            "devices": [
                {"name": f"gpu-{index}", "capacity": {"memory": {"value": value}}}
                for index, value in enumerate(memory_values, start=1)
            ],
        }
    }


def test_hardware_summary_counts_only_unallocated_devices_in_configured_scope(monkeypatch):
    slices = [
        _slice("node-a", ("gpu-a1", "0000:01:00.0"), ("gpu-a2", "0000:02:00.0")),
        _slice("node-b", ("gpu-b1", "0000:01:00.0"), ("gpu-b2", "0000:02:00.0")),
    ]
    devices = service._resource_slice_gpu_devices(slices)
    claims = [
        _claim({"driver": "gpu.intel.com", "pool": "node-a", "device": "gpu-a1"}),
        _claim({"driver": "gpu.intel.com", "pool": "node-b", "device": "gpu-b2"}),
    ]
    nodes = [{"gpuCount": 2, "gpu": True}, {"gpuCount": 2, "gpu": True}]
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "node-a")
    monkeypatch.setenv("PRISM_GPU_PCI_ALLOWLIST", "0000:01:00.0,0000:02:00.0")

    hardware = service._hardware_summary(
        nodes,
        allocated_gpu_count=2,
        gpu_devices=devices,
        allocated_gpu_devices=service._active_gpu_device_keys(claims),
    )

    assert hardware["gpuCount"] == 4
    assert hardware["availableGpuCount"] == 2
    assert hardware["configuredGpuLimit"] == 2
    assert hardware["usableGpuCount"] == 1


def test_hardware_summary_reports_usage_and_vram():
    nodes = [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 4, "gpu": True}]
    hardware = service._hardware_summary(
        nodes,
        allocated_gpu_count=1,
        vram_bytes=192 * 1024**3,
        node_usage={"cpuMillicores": 4000, "memoryBytes": 16 * 1024**3},
    )

    assert hardware["cpuUsagePercent"] == 25
    assert hardware["memoryUsagePercent"] == 25
    assert hardware["gpuUsagePercent"] == 25
    assert hardware["vramBytes"] == 192 * 1024**3
    assert hardware["vramUsagePercent"] is None


def test_node_summary_with_gpus_validates_in_pydantic():
    from llm_d_bench.cluster.models import KubernetesSummary

    node = {
        "name": "worker-1",
        "version": "v1.28.0",
        "osImage": "Ubuntu 22.04",
        "ready": True,
        "cpu": 16.0,
        "cpuUsagePercent": 25.0,
        "memoryBytes": 64 * 1024**3,
        "memoryUsagePercent": 50.0,
        "gpu": True,
        "gpuCount": 2,
        "gpuUsagePercent": 80.0,
        "vramBytes": 96 * 1024**3,
        "vramUsagePercent": 40.0,
        "role": "worker",
        "cachedImages": ["ghcr.io/llm-d/llm-d-xpu:v0.9.0"],
        "gpus": [
            {
                "id": "gpu-0",
                "index": 0,
                "name": "GPU #0",
                "pciAddress": "0000:03:00.0",
                "computeUsagePercent": 80.0,
                "vramTotalBytes": 48 * 1024**3,
                "vramUsedBytes": 20 * 1024**3,
                "vramUsagePercent": 41.6,
                "vramReadThroughputBytes": 10000,
                "vramWriteThroughputBytes": 20000,
                "vramTotalThroughputBytes": 30000,
                "vramBandwidthPercent": 15.0,
            }
        ],
    }
    overview = {
        "components": {
            "intelDevicePlugin": {"installed": True, "ready": 1, "desired": 1, "status": "ready"},
            "monitoring": {"installed": True, "ready": 5, "desired": 5, "status": "ready"},
        },
        "nodes": [node],
        "namespaces": [],
        "hardware": {},
    }
    parsed = KubernetesSummary.model_validate(overview)
    assert len(parsed.nodes) == 1
    assert parsed.nodes[0].gpus[0].pci_address == "0000:03:00.0"
    assert parsed.nodes[0].gpus[0].vram_bandwidth_percent == 15.0
    assert parsed.nodes[0].cached_images == ["ghcr.io/llm-d/llm-d-xpu:v0.9.0"]


def test_hardware_summary_prefers_prometheus_gpu_and_vram_metrics():
    hardware = service._hardware_summary(
        [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 4, "gpu": True}],
        allocated_gpu_count=1,
        vram_bytes=0,
        prometheus_metrics={
            "gpuUsagePercent": 72.34,
            "vramBytes": 96 * 1024**3,
            "vramUsagePercent": 55.55,
        },
    )

    assert hardware["gpuUsagePercent"] == 72.3
    assert hardware["vramBytes"] == 96 * 1024**3
    assert hardware["vramUsagePercent"] == 55.5


def test_hardware_summary_prefers_free_style_prometheus_memory_usage():
    hardware = service._hardware_summary(
        [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 0, "gpu": False}],
        node_usage={"memoryBytes": 16 * 1024**3},
        prometheus_metrics={"memoryUsagePercent": 6.25},
    )

    assert hardware["memoryUsagePercent"] == 6.2


def test_hardware_summary_prefers_top_style_prometheus_cpu_usage():
    hardware = service._hardware_summary(
        [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 0, "gpu": False}],
        node_usage={"cpuMillicores": 4000},
        prometheus_metrics={"cpuUsagePercent": 6.25},
    )

    assert hardware["cpuUsagePercent"] == 6.2


def test_resource_slice_gpu_vram_bytes():
    assert service._resource_slice_gpu_vram_bytes([_memory_slice("node-a", "48Gi", "512Mi")]) == int(48.5 * 1024**3)


def test_resource_slice_gpu_vram_bytes_counts_only_worker_nodes():
    slices = [_memory_slice("worker-a", "48Gi"), _memory_slice("control-plane", "48Gi")]

    assert service._resource_slice_gpu_vram_bytes(slices, {"worker-a"}) == 48 * 1024**3


def test_prometheus_device_values_filter_workers_and_dedupe_by_device():
    samples = [
        {"metric": {"node": "worker-a", "pci_bdf": "0000:01:00.0", "job": "xpumd-a"}, "value": [1, "51539607552"]},
        {"metric": {"node": "worker-a", "pci_bdf": "0000:01:00.0", "job": "xpumd-b"}, "value": [1, "51539607552"]},
        {"metric": {"node": "control-plane", "pci_bdf": "0000:02:00.0", "job": "xpumd"}, "value": [1, "51539607552"]},
    ]

    values = service._latest_device_values(samples, {"worker-a"}, {})

    assert values == {("worker-a", "0000:01:00.0"): 51539607552.0}


@pytest.mark.asyncio
async def test_prometheus_hardware_metrics_uses_compute_engine_utilization(monkeypatch):
    queries = []

    async def fake_local_port(cluster_id):
        assert cluster_id == "cluster-1"
        return 19090

    async def fake_query(_client, query):
        queries.append(query)
        if query == 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}':
            return [
                {
                    "metric": {"node": "worker-a", "pci_bdf": "0000:01:00.0"},
                    "value": [1, "0.75"],
                }
            ]
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", fake_local_port)
    monkeypatch.setattr(service, "_prometheus_query", fake_query)

    metrics = await service._prometheus_hardware_metrics(
        "cluster-1",
        {"worker-a"},
        {("worker-a", "gpu-a"): {"node": "worker-a", "pciAddress": "0000:01:00.0"}},
    )

    assert 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}' in queries
    assert 'hw_gpu_utilization_ratio{hw_gpu_task="all"}' not in queries
    assert metrics["gpuUsagePercent"] == 75
    assert metrics["nodeGpu"] == {"worker-a": 75}


def test_instance_to_node_maps_prometheus_instance_label_via_node_uname_info():
    samples = [
        {"metric": {"instance": "172.19.0.2:9100", "nodename": "worker-a"}, "value": [1, "1"]},
        {"metric": {"instance": "172.19.0.3:9100", "node": "worker-b"}, "value": [1, "1"]},
        {"metric": {"instance": "172.19.0.4:9100"}, "value": [1, "1"]},
    ]

    mapping = service._instance_to_node(samples)

    assert mapping == {"172.19.0.2:9100": "worker-a", "172.19.0.3:9100": "worker-b"}


def test_latest_node_percent_values_falls_back_to_instance_mapping_and_filters_workers():
    samples = [
        {"metric": {"instance": "172.19.0.2:9100"}, "value": [1, "38.4"]},
        {"metric": {"instance": "172.19.0.9:9100"}, "value": [1, "99.9"]},
        {"metric": {"node": "worker-b"}, "value": [1, "150"]},
    ]
    instance_to_node = {"172.19.0.2:9100": "worker-a", "172.19.0.9:9100": "control-plane"}

    values = service._latest_node_percent_values(samples, instance_to_node, {"worker-a", "worker-b"})

    assert values == {"worker-a": 38.4, "worker-b": 100.0}


def test_annotate_node_usage_falls_back_to_prometheus_cpu_and_memory_when_metrics_api_missing():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {},
        {"nodeCpu": {"worker-a": 38.4}, "nodeMemory": {"worker-a": 21.2}},
    )

    assert annotated[0]["cpuUsagePercent"] == 38.4
    assert annotated[0]["memoryUsagePercent"] == 21.2


def test_annotate_node_usage_prefers_free_style_prometheus_memory_usage():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {"worker-a": {"memoryBytes": 8 * 1024**3}},
        {"nodeMemory": {"worker-a": 6.2}},
    )

    assert annotated[0]["memoryUsagePercent"] == 6.2


def test_annotate_node_usage_prefers_top_style_prometheus_cpu_usage():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {"worker-a": {"cpuMillicores": 2000}},
        {"nodeCpu": {"worker-a": 6.2}},
    )

    assert annotated[0]["cpuUsagePercent"] == 6.2


def test_annotate_node_usage_adds_per_worker_utilization():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 2, "gpu": True}]
    annotated = service._annotate_node_usage(
        nodes,
        {"worker-a": {"cpuMillicores": 2000, "memoryBytes": 8 * 1024**3}},
        {
            "nodeGpu": {"worker-a": 62.34},
            "nodeVram": {"worker-a": {"totalBytes": 48 * 1024**3, "usedBytes": 12 * 1024**3}},
        },
    )

    assert annotated[0]["cpuUsagePercent"] == 25
    assert annotated[0]["memoryUsagePercent"] == 25
    assert annotated[0]["gpuUsagePercent"] == 62.3
    assert annotated[0]["vramBytes"] == 48 * 1024**3
    assert annotated[0]["vramUsagePercent"] == 25


def test_annotate_node_usage_adds_kubelet_disk_stats():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {},
        {},
        disk_usage_by_node={"worker-a": {"capacityBytes": 470 * 1024**3, "usedBytes": 470 * 1024**3}},
    )

    assert annotated[0]["diskBytes"] == 470 * 1024**3
    assert annotated[0]["diskUsedBytes"] == 470 * 1024**3
    assert annotated[0]["diskUsagePercent"] == 100.0


def test_annotate_node_usage_disk_defaults_when_kubelet_unreachable():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(nodes, {}, {})

    assert annotated[0]["diskBytes"] == 0
    assert annotated[0]["diskUsedBytes"] is None
    assert annotated[0]["diskUsagePercent"] is None


def test_hardware_summary_aggregates_disk_usage_across_nodes():
    nodes = [
        {
            "name": "worker-a",
            "cpu": 8,
            "memoryBytes": 0,
            "gpuCount": 0,
            "gpu": False,
            "diskBytes": 100,
            "diskUsedBytes": 100,
            "diskUsagePercent": 100.0,
        },
        {
            "name": "worker-b",
            "cpu": 8,
            "memoryBytes": 0,
            "gpuCount": 0,
            "gpu": False,
            "diskBytes": 100,
            "diskUsedBytes": 0,
            "diskUsagePercent": 0.0,
        },
    ]

    hardware = service._hardware_summary(nodes)

    assert hardware["diskBytes"] == 200
    assert hardware["diskUsagePercent"] == 50.0


def test_hardware_summary_disk_usage_percent_none_when_no_node_reports_disk():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 0, "gpuCount": 0, "gpu": False}]

    hardware = service._hardware_summary(nodes)

    assert hardware["diskBytes"] == 0
    assert hardware["diskUsagePercent"] is None


@pytest.mark.asyncio
async def test_node_disk_usage_parses_kubelet_summary(monkeypatch):
    async def fake_kubectl(command, timeout=10, cluster_id=None):
        assert command[-1] == "/api/v1/nodes/worker-a/proxy/stats/summary"
        return CommandResult(
            argv=command,
            returncode=0,
            stdout=json.dumps({"node": {"fs": {"capacityBytes": 1000, "usedBytes": 1000}}}),
            stderr="",
        )

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    result = await service._node_disk_usage("cluster-1", {"worker-a"})

    assert result == {"worker-a": {"capacityBytes": 1000, "usedBytes": 1000}}


@pytest.mark.asyncio
async def test_node_disk_usage_skips_node_when_kubelet_call_fails(monkeypatch):
    async def fake_kubectl(command, timeout=10, cluster_id=None):
        raise TimeoutError("kubectl timed out")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    result = await service._node_disk_usage("cluster-1", {"worker-a"})

    assert result == {}


def test_hardware_summary_uses_node_with_pci_identity_not_pci_alone(monkeypatch):
    slices = [
        _slice("node-a", ("gpu-a1", "0000:01:00.0")),
        _slice("node-b", ("gpu-b1", "0000:01:00.0")),
    ]
    devices = service._resource_slice_gpu_devices(slices)
    monkeypatch.delenv("PRISM_K8S_TARGET_NODE", raising=False)
    monkeypatch.setenv("PRISM_GPU_PCI_ALLOWLIST", "0000:01:00.0")

    hardware = service._hardware_summary(
        [{"gpuCount": 1, "gpu": True}, {"gpuCount": 1, "gpu": True}],
        gpu_devices=devices,
        allocated_gpu_devices={("node-a", "gpu-a1")},
    )

    assert hardware["configuredGpuLimit"] == 2
    assert hardware["usableGpuCount"] == 1


@pytest.mark.asyncio
async def test_model_secret_discovery_returns_external_coordinates_without_values(monkeypatch):
    """Every namespace's HF_TOKEN secret is returned regardless of its name
    -- namespace names are user-chosen (including via a deployment's custom
    ``namespace_policy.prefix``, see ``build_namespace_factory``), so
    filtering by name/prefix is unreliable and previously hid legitimate
    secrets (reproduced live: a secret in ``llm-d-bench-storage-<cluster
    id>``, created by the Create Cluster wizard's Model Cache step, did not
    show up when choosing a token to prewarm a model). Secret values must
    never be included in the response, and secrets without HF_TOKEN must be
    skipped."""
    payload = {
        "items": [
            {
                "metadata": {"namespace": "models", "name": "huggingface", "creationTimestamp": "now"},
                "data": {"HF_TOKEN": "must-not-be-returned"},
            },
            {
                "metadata": {
                    "namespace": f"{service.cluster_settings.namespace_prefix}failed-deployment",
                    "name": "llm-d-hf-token",
                },
                "data": {"HF_TOKEN": "copied-deployment-token"},
            },
            {
                "metadata": {
                    "namespace": "llm-d-bench-storage-e97867eb",
                    "name": "hf-token-544aeb47",
                    "creationTimestamp": "now",
                },
                "data": {"HF_TOKEN": "value"},
            },
            {"metadata": {"namespace": "models", "name": "unrelated"}, "data": {"OTHER": "value"}},
        ]
    }

    async def fake_kubectl(*_args, **_kwargs):
        return CommandResult(("kubectl",), 0, json.dumps(payload), "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
    result = await service.list_model_secrets(SimpleNamespace(id="cluster"))

    assert result == [
        {"namespace": "llm-d-bench-failed-deployment", "name": "llm-d-hf-token", "createdAt": ""},
        {"namespace": "llm-d-bench-storage-e97867eb", "name": "hf-token-544aeb47", "createdAt": "now"},
        {"namespace": "models", "name": "huggingface", "createdAt": "now"},
    ]


# --- Create Cluster wizard: proxy settings + HF token secret creation ---


def test_registry_round_trips_proxy_and_version_refs(tmp_path, monkeypatch):
    """Cluster metadata should persist proxy/llm-d ref fields across a save/load cycle."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster(
        "c1",
        "",
        "kubeconfig: {}",
        proxy_mode="custom",
        http_proxy="http://proxy:3128",
        https_proxy="http://proxy:3128",
        no_proxy="localhost",
        llm_d_ref="v0.2.0",
        llm_d_benchmark_ref="abc1234",
    )
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded.proxy_mode == "custom"
    assert reloaded.http_proxy == "http://proxy:3128"
    assert reloaded.no_proxy == "localhost"
    assert reloaded.llm_d_ref == "v0.2.0"
    assert reloaded.llm_d_benchmark_ref == "abc1234"


def test_registry_update_cluster_patches_only_given_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("c1", "desc", "kubeconfig: {}")
    updated = registry.update_cluster(cluster.id, llm_d_ref="main")
    assert updated.llm_d_ref == "main"
    assert updated.name == "c1"
    assert updated.description == "desc"
    assert updated.proxy_mode == "auto"
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded.llm_d_ref == "main"


def test_create_cluster_rejects_empty_custom_proxy():
    with pytest.raises(ClusterOverviewError) as excinfo:
        service.create_cluster("c1", "", "kubeconfig: {}", proxy_mode="custom")
    assert excinfo.value.code == "invalid_proxy_config"


def test_update_cluster_settings_rejects_empty_custom_proxy(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    with pytest.raises(ClusterOverviewError) as excinfo:
        service.update_cluster_settings("cluster-1", proxy_mode="custom")
    assert excinfo.value.code == "invalid_proxy_config"


def test_create_cluster_rejects_duplicate_name(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    registry.create_cluster("prod-cluster", "", "kubeconfig: {}")
    with pytest.raises(ClusterOverviewError) as excinfo:
        registry.create_cluster("Prod-Cluster", "", "kubeconfig: {}")  # different case, still a duplicate
    assert excinfo.value.code == "cluster_name_conflict"
    assert excinfo.value.status_code == 409


def test_create_cluster_allows_duplicate_name_against_a_draft(tmp_path, monkeypatch):
    """A name collision with an in-progress (or abandoned) draft cluster must
    NOT be rejected: drafts are invisible scratch state that may never be
    cleaned up (e.g. the wizard tab was closed before its delete request
    landed), so counting them here would let a stale draft permanently
    squat a name and block real recreation with a spurious conflict."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    registry.create_cluster("wip-cluster", "", "kubeconfig: {}", draft=True)
    created = registry.create_cluster("wip-cluster", "", "kubeconfig: {}")
    assert created.name == "wip-cluster"


def test_update_cluster_rejects_finalizing_draft_into_duplicate_name(tmp_path, monkeypatch):
    """If two drafts raced to the same name, the second one to finalize
    (draft False) must still be rejected instead of silently producing two
    real clusters with the same name."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    first = registry.create_cluster("wip-cluster", "", "kubeconfig: {}", draft=True)
    second = registry.create_cluster("wip-cluster", "", "kubeconfig: {}", draft=True)
    registry.update_cluster(first.id, draft=False)
    with pytest.raises(ClusterOverviewError) as excinfo:
        registry.update_cluster(second.id, draft=False)
    assert excinfo.value.code == "cluster_name_conflict"


def test_update_cluster_rejects_rename_to_duplicate_name(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    registry.create_cluster("existing", "", "kubeconfig: {}")
    other = registry.create_cluster("other", "", "kubeconfig: {}")
    with pytest.raises(ClusterOverviewError) as excinfo:
        registry.update_cluster(other.id, name="existing")
    assert excinfo.value.code == "cluster_name_conflict"


def test_update_cluster_allows_renaming_to_its_own_current_name(tmp_path, monkeypatch):
    """Re-saving a cluster's own unchanged name must not trip the uniqueness
    check against itself (e.g. an Edit modal that always sends `name`)."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("my-cluster", "", "kubeconfig: {}")
    updated = registry.update_cluster(cluster.id, name="my-cluster", description="new description")
    assert updated.name == "my-cluster"
    assert updated.description == "new description"


def _clear_proxy_env(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_resolve_proxy_env_uses_custom_cluster_values(monkeypatch):
    cluster = registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        proxy_mode="custom",
        http_proxy="http://custom:3128",
        https_proxy=None,
        no_proxy=None,
    )
    monkeypatch.setattr(registry, "get_cluster", lambda cluster_id: cluster)
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://ambient:3128")
    assert service.resolve_proxy_env("cluster-1") == {"HTTP_PROXY": "http://custom:3128"}


def test_resolve_proxy_env_falls_back_to_process_env_when_auto(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "get_cluster", lambda cluster_id: cluster)
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://ambient:3128")
    assert service.resolve_proxy_env("cluster-1") == {"HTTP_PROXY": "http://ambient:3128"}


def test_resolve_proxy_env_returns_empty_for_unknown_cluster(monkeypatch):
    monkeypatch.setattr(registry, "get_cluster", lambda cluster_id: None)
    _clear_proxy_env(monkeypatch)
    assert service.resolve_proxy_env("unknown") == {}


@pytest.mark.asyncio
async def test_create_hf_token_secret_rejects_duplicate_name(monkeypatch):
    cluster = SimpleNamespace(id="cluster-1")

    async def fake_kubectl(command, *, cluster_id, timeout):
        assert command[:2] == ["get", "secret"]
        return CommandResult(("kubectl",), 0, "secret/hf-token-existing", "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.create_hf_token_secret(cluster, "default", "hf-token-existing", "hf_xxx")
    assert excinfo.value.code == "secret_already_exists"


@pytest.mark.asyncio
async def test_create_hf_token_secret_creates_when_absent(monkeypatch):
    cluster = registry.create_cluster("cluster-1", "", "kubeconfig: {}")
    created: dict[str, object] = {}

    async def fake_kubectl(command, *, cluster_id, timeout):
        return CommandResult(("kubectl",), 0, "", "")

    class FakeRunner:
        async def run(self, argv, *, input=None, timeout=None):  # noqa: A002 - mirrors CommandRunner's public keyword
            if argv[1:3] == ["create", "namespace"]:
                return CommandResult(("kubectl",), 0, "apiVersion: v1\nkind: Namespace\n", "")
            if argv[1] == "apply":
                return CommandResult(("kubectl",), 0, "", "")
            if argv[1] == "create" and argv[2] == "-f":
                created["manifest"] = json.loads(input)
                return CommandResult(("kubectl",), 0, "secret/hf-token-new created", "")
            raise AssertionError(f"unexpected kubectl invocation: {argv}")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
    monkeypatch.setattr(service, "scoped_runner", lambda cluster_id: FakeRunner())

    ref = await service.create_hf_token_secret(cluster, "default", "hf-token-new", "hf_secret_value")

    assert ref == {"namespace": "default", "name": "hf-token-new"}
    assert created["manifest"]["metadata"] == {"name": "hf-token-new", "namespace": "default"}
    assert created["manifest"]["stringData"] == {"HF_TOKEN": "hf_secret_value"}
    # Recorded on the cluster so Deploy/Evaluate can default to reusing it later.
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded.hf_token_secret_namespace == "default"  # noqa: S105
    assert reloaded.hf_token_secret_name == "hf-token-new"  # noqa: S105 -- k8s Secret name, not a credential


def test_component_status_exposes_per_profile_device_plugins():
    resources = [
        {"kind": "DaemonSet", "metadata": {"name": "intel-gpu-plugin"},
         "status": {"desiredNumberScheduled": 1, "numberReady": 1}},
        {"kind": "DaemonSet", "metadata": {"name": "nvidia-device-plugin-daemonset"},
         "status": {"desiredNumberScheduled": 1, "numberReady": 1}},
    ]
    status = service.component_status(resources)
    assert status["intelDevicePlugin"]["installed"] is True
    assert status["acceleratorDevicePlugins"]["intel-xpu"]["ready"] == 1
    assert status["acceleratorDevicePlugins"]["nvidia"]["ready"] == 1

    from llm_d_bench.cluster.models import ComponentsSummary

    ComponentsSummary.model_validate({
        **status,
        "monitoring": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
    })


def test_hardware_summary_exposes_per_vendor_buckets():
    nodes = [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 4, "gpu": True}]
    hardware = service._hardware_summary(nodes, gpu_by_profile={"intel-xpu": 4})
    assert hardware["gpuCount"] == 4
    assert hardware["totalGpuCount"] == 4
    assert hardware["accelerators"] == [{"id": "intel-xpu", "gpuCount": 4}]


def test_node_summary_counts_extended_resources_per_profile_and_excludes_monitor_marker():
    node = {
        "metadata": {"name": "node-a", "labels": {}},
        "spec": {},
        "status": {
            "capacity": {"gpu.intel.com/xe": "8", "gpu.intel.com/monitoring": "1"},
            "conditions": [],
        },
    }
    summary = service._node_summary(node)
    assert summary["gpuCount"] == 8
    assert summary["gpuByProfile"] == {"intel-xpu": 8}


def test_node_summary_attributes_dra_slices_to_their_profile():
    node = {"metadata": {"name": "node-a", "labels": {}}, "spec": {}, "status": {"conditions": []}}
    summary = service._node_summary(node, {"node-a": 3}, {"node-a": {"intel-xpu": 3}})
    assert summary["gpuCount"] == 3
    assert summary["gpuByProfile"] == {"intel-xpu": 3}
