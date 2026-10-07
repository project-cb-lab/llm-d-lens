"""Tests for accelerator observability discovery, preflight and API contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.monitoring.accelerator.intel_gpu import (
    IntelGpuProvider,
    _availability,
    _compute_engine_dashboard,
)
from llm_d_bench.monitoring.accelerator.models import (
    AcceleratorInstallRequest,
    AcceleratorStatusResponse,
    GpuAccess,
)
from llm_d_bench.monitoring.accelerator.operations import (
    AcceleratorOperationManager,
    AcceleratorOperationStore,
)
from llm_d_bench.monitoring.accelerator.service import (
    _find_service,
    _grafana_dashboard_path,
    _service_port,
    _slugify,
    get_links,
    get_status,
    validate_namespace,
)
from llm_d_bench.monitoring.cluster_stack.models import (
    ClusterStackStatusResponse,
    ClusterSummary,
    HelmReleaseSummary,
)
from llm_d_bench.utils import kubernetes as kubernetes_module
from llm_d_bench.utils.shell import CommandResult


def _result(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(tuple(argv), returncode, stdout, stderr)


def _patch_runner(monkeypatch, runner: FakeRunner) -> FakeRunner:
    """Route the provider's scoped runner (used by kubernetes/helm helpers) to a fake."""
    monkeypatch.setattr(kubernetes_module, "scoped_runner", lambda cluster_id=None: runner)
    return runner


class FakeRunner:
    def __init__(
        self,
        *,
        installed: bool,
        access_mode: GpuAccess = "dra",
        gpu_nodes: bool = True,
        degraded: bool = False,
    ) -> None:
        self.installed = installed
        self.access_mode = access_mode
        self.gpu_nodes = gpu_nodes
        self.degraded = degraded
        self.labeled_nodes: list[str] = []

    def executable(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
        del timeout
        command = " ".join(argv)
        if command == "kubectl config current-context":
            return _result(argv, "kind-test\n")
        if command == "kubectl cluster-info":
            return _result(argv, "Kubernetes control plane is running")
        if command.startswith("helm status xpumd"):
            if not self.installed:
                return _result(argv, stderr="Error: release: not found", returncode=1)
            return _result(argv, json.dumps({"version": 2, "info": {"status": "deployed"}}))
        if command.startswith("helm list"):
            if not self.installed:
                return _result(argv, json.dumps([]))
            return _result(argv, json.dumps([{"chart": "xpumd-2.1.0"}]))
        if command.startswith("helm get values"):
            return _result(argv, json.dumps({"gpuAccess": self.access_mode}))
        if "app.kubernetes.io/name=xpumd" in command and "daemonsets,services" in command:
            items = []
            if self.installed:
                items = [self._daemonset("xpumd", ready=True)]
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get servicemonitors"):
            items = []
            if self.installed and not self.degraded:
                items = [
                    {
                        "kind": "ServiceMonitor",
                        "metadata": {"name": "xpumd", "labels": {"app.kubernetes.io/name": "xpumd"}},
                    }
                ]
            return _result(argv, json.dumps({"items": items}))
        if "grafana_dashboard=1" in command:
            items = []
            if self.installed and not self.degraded:
                items = [{"kind": "ConfigMap", "metadata": {"name": "xpumd-dashboard"}}]
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get nodes"):
            items = []
            if self.gpu_nodes:
                items = [
                    {
                        "metadata": {"name": "gpu-node-1"},
                        "status": {"allocatable": {"gpu.intel.com/monitoring": "1"}},
                    }
                ]
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get resourceslices"):
            items = [{"spec": {"driver": "gpu.intel.com", "nodeName": "gpu-node-1"}}] if self.gpu_nodes else []
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl label node"):
            self.labeled_nodes.append(argv[3])
            return _result(argv, "node/gpu-node-1 labeled\n")
        if command == "kubectl get daemonsets --all-namespaces -o json":
            return _result(argv, json.dumps({"items": self._access_daemonsets()}))
        if command.startswith("kubectl get namespace"):
            return _result(argv, stderr="not found", returncode=1)
        raise AssertionError(f"Unexpected command: {command}")

    def _access_daemonsets(self) -> list[dict]:
        if self.access_mode == "dra":
            return [
                {
                    "kind": "DaemonSet",
                    "metadata": {"name": "intel-gpu-resource-driver", "namespace": "intel-gpu-resource-driver"},
                    "status": {"desiredNumberScheduled": 1, "numberReady": 1},
                }
            ]
        return [
            {
                "kind": "DaemonSet",
                "metadata": {
                    "name": "intel-gpu-plugin",
                    "namespace": "intel-gpu-plugin",
                    "labels": {"app": "intel-gpu-plugin"},
                },
                "spec": {"template": {"spec": {"containers": [{"args": ["-enable-monitoring"]}]}}},
                "status": {"desiredNumberScheduled": 1, "numberReady": 1},
            }
        ]

    @staticmethod
    def _daemonset(name: str, *, ready: bool) -> dict:
        return {
            "kind": "DaemonSet",
            "metadata": {"name": name},
            "status": {"desiredNumberScheduled": 1, "numberReady": 1 if ready else 0},
        }


@pytest.mark.asyncio
async def test_discovery_reports_absent(monkeypatch):
    provider = IntelGpuProvider()
    _patch_runner(monkeypatch, FakeRunner(installed=False))
    status = await provider.discover_status("intel-xpumd", None)

    assert status.status == "absent"
    assert status.cluster_reachable is True
    assert status.context == "kind-test"
    assert status.release.name == "xpumd"
    assert status.access_modes[0].mode == "dra"
    assert status.access_modes[0].available is True


@pytest.mark.asyncio
async def test_discovery_reports_ready(monkeypatch):
    provider = IntelGpuProvider()
    _patch_runner(monkeypatch, FakeRunner(installed=True))
    status = await provider.discover_status("intel-xpumd", None)

    assert status.status == "ready"
    components = {component.name: component for component in status.components}
    assert components["xpumd"].status == "ready"
    assert components["prometheus_service_monitor"].status == "ready"
    assert components["grafana_dashboard"].status == "ready"
    assert components["gpu_nodes"].status == "ready"
    assert status.access_mode == "dra"


@pytest.mark.asyncio
async def test_access_mode_detection_plugin(monkeypatch):
    provider = IntelGpuProvider()
    _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="plugin"))
    status = await provider.discover_status("intel-xpumd", None)

    plugin = next(mode for mode in status.access_modes if mode.mode == "plugin")
    assert plugin.detected is True
    assert plugin.available is True


def test_plugin_requires_enable_monitoring_flag():
    plugin_daemonset = {
        "kind": "DaemonSet",
        "metadata": {"name": "intel-gpu-plugin", "namespace": "intel-gpu-plugin"},
        "spec": {"template": {"spec": {"containers": [{"name": "intel-gpu-plugin"}]}}},
        "status": {"desiredNumberScheduled": 1, "numberReady": 1},
    }

    availability = _availability("plugin", [plugin_daemonset])

    assert availability.detected is True
    assert availability.available is False
    assert "-enable-monitoring" in availability.message


@pytest.mark.asyncio
async def test_preflight_blocks_on_missing_monitoring_stack(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="failed"),
        status="degraded",
        message="degraded",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=False))
    result = await provider.preflight(AcceleratorInstallRequest(), None, monitoring_status=monitoring)

    checks = {check.name: check for check in result.checks}
    assert checks["monitoring_stack"].passed is False
    assert result.allowed is False


@pytest.mark.asyncio
async def test_preflight_gpu_nodes_presence_check_is_blocking(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=False, gpu_nodes=True))
    result = await provider.preflight(
        AcceleratorInstallRequest(),
        None,
        monitoring_status=monitoring,
    )

    checks = {check.name: check for check in result.checks}
    # Hardware presence is a blocking gate; the access-mode label stays a warning.
    assert checks["gpu_nodes"].blocking is True
    assert checks["access_label"].passed is False
    assert checks["access_label"].blocking is False
    assert result.status == "absent"
    assert result.command_preview[0] == "helm"


@pytest.mark.asyncio
async def test_intel_hardware_presence_probe_detects_label_dra_or_extended_resource(monkeypatch):
    from llm_d_bench.monitoring.accelerator import intel_gpu

    present = {"label": False, "dra": False, "extended": False}

    async def fake_list_resources(kind: str, **kwargs):
        if kind == "nodes" and kwargs.get("selector"):
            return [{"metadata": {"name": "n"}}] if present["label"] else []
        if kind == "resourceslices":
            return [{"spec": {"driver": "gpu.intel.com"}}] if present["dra"] else []
        if kind == "nodes":
            if present["extended"]:
                return [{"status": {"allocatable": {"gpu.intel.com/xe": "8"}}}]
            return []
        return []

    monkeypatch.setattr(intel_gpu, "list_resources", fake_list_resources)
    assert await intel_gpu._intel_hardware_present(None) is False
    for signal in ("label", "dra", "extended"):
        present[signal] = True
        assert await intel_gpu._intel_hardware_present(None) is True
        present[signal] = False


@pytest.mark.asyncio
async def test_preflight_access_label_only_for_dra(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="plugin", gpu_nodes=False))
    result = await provider.preflight(
        AcceleratorInstallRequest(access_mode="plugin"),
        None,
        monitoring_status=monitoring,
    )

    check_names = {check.name for check in result.checks}
    assert "access_label" not in check_names
    assert "gpu_nodes" in check_names  # gpu_nodes is shown for plugin too


@pytest.mark.asyncio
async def test_prepare_nodes_labels_gpu_nodes_for_dra(monkeypatch):
    provider = IntelGpuProvider()
    runner = _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="dra", gpu_nodes=True))
    await provider.prepare_nodes(None, access_mode="dra")

    assert runner.labeled_nodes == ["gpu-node-1"]


@pytest.mark.asyncio
async def test_prepare_nodes_labels_gpu_nodes_for_plugin(monkeypatch):
    provider = IntelGpuProvider()
    runner = _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="plugin", gpu_nodes=True))
    await provider.prepare_nodes(None, access_mode="plugin")

    assert runner.labeled_nodes == ["gpu-node-1"]


@pytest.mark.asyncio
async def test_preflight_allows_reconcile_when_degraded(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=True, degraded=True))
    status = await provider.discover_status("intel-xpumd", None)
    assert status.status == "degraded"

    result = await provider.preflight(AcceleratorInstallRequest(), None, monitoring_status=monitoring)

    checks = {check.name: check for check in result.checks}
    assert checks["stack_state"].passed is True
    assert result.allowed is True
    assert result.status == "degraded"
    assert result.command_preview[:3] == ["helm", "upgrade", "--install"]


@pytest.mark.asyncio
async def test_preflight_allows_reconcile_when_ready(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=True))
    status = await provider.discover_status("intel-xpumd", None)
    assert status.status == "ready"

    result = await provider.preflight(AcceleratorInstallRequest(), None, monitoring_status=monitoring)

    checks = {check.name: check for check in result.checks}
    assert checks["stack_state"].passed is True
    assert result.allowed is True
    assert result.status == "ready"
    assert result.command_preview[:3] == ["helm", "upgrade", "--install"]


def test_install_argv_wires_prometheus_exporter():
    provider = IntelGpuProvider()
    argv = provider.install_argv(
        AcceleratorInstallRequest(),
        monitoring_namespace="llm-d-monitoring",
        monitoring_release="llmd",
    )

    assert "config.service.pipelines.metrics.exporters={intel_xpu_info,prometheus}" in argv


def test_observability_link_service_discovery():
    services = [
        {"metadata": {"name": "llmd-grafana"}, "spec": {"ports": [{"port": 80}]}},
        {
            "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
            "spec": {"ports": [{"port": 9090}, {"port": 8080}]},
        },
        {"metadata": {"name": "llmd-prometheus-node-exporter"}, "spec": {"ports": [{"port": 9100}]}},
    ]

    prometheus = _find_service(services, lambda name: name.endswith("-prometheus"))
    grafana = _find_service(services, lambda name: name.endswith("-grafana"))

    assert prometheus is not None
    assert prometheus["metadata"]["name"] == "llmd-kube-prometheus-stack-prometheus"
    assert _service_port(prometheus, 9090) == 9090
    assert grafana is not None
    assert grafana["metadata"]["name"] == "llmd-grafana"
    assert _service_port(grafana, 80) == 80


def test_slugify_matches_grafana_title():
    assert _slugify("Intel XPU Manager v2 Exporter") == "intel-xpu-manager-v2-exporter"
    assert _slugify("GPU  Utilization / Temp!") == "gpu-utilization-temp"


def test_compute_engine_dashboard_only_changes_gpu_overview_panels():
    dashboard = {
        "panels": [
            {
                "title": "GPU Utilization",
                "targets": [{"expr": 'avg by (pci_bdf) (hw_gpu_utilization_ratio{node="$Node"})'}],
            },
            {
                "title": "Engine Group Utilization",
                "targets": [{"expr": 'avg by (pci_bdf,hw_gpu_task) (hw_gpu_utilization_ratio{node="$Node"})'}],
            },
        ]
    }

    normalized, changed = _compute_engine_dashboard(json.dumps(dashboard))
    result = json.loads(normalized)

    assert changed is True
    assert result["panels"][0]["targets"][0]["expr"] == (
        'avg by (pci_bdf) (hw_gpu_utilization_ratio{hw_gpu_task="compute-all",node="$Node"})'
    )
    assert result["panels"][1] == dashboard["panels"][1]


@pytest.mark.asyncio
async def test_grafana_dashboard_path_resolves_uid_from_configmap(monkeypatch):
    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        assert resource == "configmaps"
        assert namespace == "llm-d-monitoring"
        assert selector == "grafana_dashboard=1"
        assert cluster_id == "test-cluster"
        return [
            {
                "kind": "ConfigMap",
                "metadata": {"name": "xpumd-dashboard"},
                "data": {
                    "dashboard.json": json.dumps(
                        {
                            "uid": "9VxbZDPink",
                            "title": "Intel XPU Manager v2 Exporter",
                        }
                    )
                },
            }
        ]

    monkeypatch.setattr("llm_d_bench.monitoring.accelerator.service.list_resources", fake_list_resources)

    path = await _grafana_dashboard_path("llm-d-monitoring", "test-cluster")
    assert path == "/d/9VxbZDPink/intel-xpu-manager-v2-exporter"


@pytest.mark.asyncio
async def test_grafana_dashboard_path_returns_none_without_dashboard(monkeypatch):
    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        return []

    monkeypatch.setattr("llm_d_bench.monitoring.accelerator.service.list_resources", fake_list_resources)

    assert await _grafana_dashboard_path("llm-d-monitoring", "test-cluster") is None


@pytest.mark.asyncio
async def test_status_without_cluster_does_not_query_ambient(monkeypatch):
    def unexpected(cluster_id=None):
        raise AssertionError(f"scoped_runner should not be called without a cluster (got {cluster_id!r})")

    monkeypatch.setattr(kubernetes_module, "scoped_runner", unexpected)
    status = await get_status("intel_gpu", "intel-xpumd", cluster_id=None)

    assert status.status == "unknown"
    assert status.cluster_reachable is False
    assert status.message == "No target cluster selected"
    assert status.stale is True


@pytest.mark.asyncio
async def test_links_without_cluster_returns_empty(monkeypatch):
    def unexpected(cluster_id=None):
        raise AssertionError(f"scoped_runner should not be called without a cluster (got {cluster_id!r})")

    monkeypatch.setattr(kubernetes_module, "scoped_runner", unexpected)
    links = await get_links("intel_gpu", cluster_id=None)

    assert links.accelerator == "intel_gpu"
    assert links.links == []


def test_install_request_forbids_unknown_fields_and_validates_namespace():
    with pytest.raises(ValueError):
        AcceleratorInstallRequest.model_validate({"namespace": "Invalid_Name"})
    with pytest.raises(ValueError):
        AcceleratorInstallRequest.model_validate({"unexpected": True})
    assert validate_namespace("intel-xpumd") == "intel-xpumd"


def test_shared_api_registers_accelerator_routes():
    client = TestClient(app)
    paths = client.get("/api/simulation/openapi.json").json()["paths"]

    assert "/api/v1/monitoring/accelerators" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/status" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/installations" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/installations/preflight" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/operations/{operation_id}" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/links" in paths


def test_operation_path_cannot_escape_store(tmp_path: Path):
    store = AcceleratorOperationStore(tmp_path)
    assert store.load("../../etc/passwd") is None

    manager = AcceleratorOperationManager(store)
    from llm_d_bench.monitoring.accelerator.models import AcceleratorOperationResponse

    operation = AcceleratorOperationResponse(
        operation_id="a" * 32,
        status="running",
        phase="installing_chart",
        accelerator="intel_gpu",
        access_mode="dra",
        namespace="intel-xpumd",
    )
    manager._append(operation, "\x1b[32madmin password: secret-value\x1b[0m")
    assert operation.logs[0].message == "admin password: [REDACTED]"


@pytest.mark.asyncio
async def test_install_operation_requires_ready_verification(tmp_path: Path):
    script = tmp_path / "install.sh"
    script.write_text(
        "#!/bin/sh\necho 'Installing xpumd chart'\necho 'admin password: secret-value'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o100)
    manager = AcceleratorOperationManager(AcceleratorOperationStore(tmp_path / "operations"))
    phases = []

    async def configure_monitoring() -> None:
        phases.append("configure")

    async def verify(namespace: str) -> AcceleratorStatusResponse:
        phases.append("verify")
        return AcceleratorStatusResponse(
            accelerator="intel_gpu",
            cluster_reachable=True,
            namespace=namespace,
            release=HelmReleaseSummary(name="xpumd", status="deployed"),
            status="ready",
            message="ready",
            observed_at=datetime.now(UTC),
        )

    created = await manager.create(
        AcceleratorInstallRequest(),
        context="kind-test",
        argv=[str(script)],
        verifier=verify,
        idempotency_key="request-1",
        post_install=configure_monitoring,
    )
    for _ in range(50):
        completed = manager.get(created.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status == "succeeded"
    assert phases == ["configure", "verify"]
    assert completed.phase == "completed"
    assert all("secret-value" not in entry.message for entry in completed.logs)
    persisted = manager.store.load(created.operation_id)
    assert persisted is not None
    assert persisted.status == "succeeded"


def test_install_request_requires_registered_accelerator():
    from pydantic import ValidationError

    assert AcceleratorInstallRequest(accelerator="intel_gpu").accelerator == "intel_gpu"
    with pytest.raises(ValidationError):
        AcceleratorInstallRequest(accelerator="not-a-real-accelerator")
