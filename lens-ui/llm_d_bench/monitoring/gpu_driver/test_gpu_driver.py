"""Tests for the GPU driver (DRA / device plugin) installer API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.monitoring.gpu_driver import service as gpu_driver_service
from llm_d_bench.utils import kubernetes as kubernetes_module
from llm_d_bench.utils.shell import CommandResult, CommandTimeoutError

client = TestClient(app)


def _result(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(tuple(argv), returncode, stdout, stderr)


def _daemonset(name: str, *, namespace: str = "default", labels: dict | None = None, ready: bool = True) -> dict:
    return {
        "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": namespace, "labels": labels or {}},
        "status": {"desiredNumberScheduled": 1, "numberReady": 1 if ready else 0},
    }


class FakeRunner:
    def __init__(
        self,
        *,
        daemonsets: list[dict] | None = None,
        cluster_info_ok: bool = True,
        gpu_labelled_nodes: list[dict] | None = None,
    ) -> None:
        self.daemonsets = daemonsets or []
        self.cluster_info_ok = cluster_info_ok
        self.applied: list[str] = []
        # By default a node already carries the NFD GPU label, so hardware
        # detection succeeds immediately without any polling delay in tests.
        self.gpu_labelled_nodes = (
            [{"metadata": {"name": "node-1"}}] if gpu_labelled_nodes is None else gpu_labelled_nodes
        )

    def executable(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
        del timeout
        command = " ".join(argv)
        if command == "kubectl cluster-info":
            if not self.cluster_info_ok:
                return _result(argv, stderr="unreachable", returncode=1)
            return _result(argv, "Kubernetes control plane is running")
        if command.startswith("kubectl get daemonsets") and "--all-namespaces" in command:
            return _result(argv, json.dumps({"items": self.daemonsets}))
        if command.startswith("kubectl get nodes"):
            return _result(argv, json.dumps({"items": self.gpu_labelled_nodes}))
        if command.startswith("kubectl apply -k"):
            self.applied.append(command)
            return _result(argv, "applied")
        return _result(argv, returncode=1, stderr=f"unexpected command: {command}")


def _patch_runner(monkeypatch, runner: FakeRunner) -> FakeRunner:
    monkeypatch.setattr(kubernetes_module, "scoped_runner", lambda cluster_id=None: runner)
    return runner


@pytest.mark.asyncio
async def test_status_reports_not_installed_when_no_daemonset(monkeypatch):
    _patch_runner(monkeypatch, FakeRunner(daemonsets=[]))
    status = await gpu_driver_service.get_status("dra", cluster_id=None, hardware="intel-xpu")
    assert status.installed is False
    assert status.ready is False
    assert status.cluster_reachable is True


@pytest.mark.asyncio
async def test_status_reports_ready_when_dra_daemonset_matches_regardless_of_namespace(monkeypatch):
    runner = FakeRunner(daemonsets=[_daemonset("intel-gpu-resource-driver-kubelet-plugin", namespace="some-other-ns")])
    _patch_runner(monkeypatch, runner)
    status = await gpu_driver_service.get_status("dra", cluster_id=None, hardware="intel-xpu")
    assert status.installed is True
    assert status.ready is True


@pytest.mark.asyncio
async def test_status_reports_installed_but_not_ready(monkeypatch):
    runner = FakeRunner(daemonsets=[_daemonset("intel-gpu-plugin", labels={"app": "intel-gpu-plugin"}, ready=False)])
    _patch_runner(monkeypatch, runner)
    status = await gpu_driver_service.get_status("plugin", cluster_id=None, hardware="intel-xpu")
    assert status.installed is True
    assert status.ready is False


@pytest.mark.asyncio
async def test_status_unreachable_cluster(monkeypatch):
    _patch_runner(monkeypatch, FakeRunner(cluster_info_ok=False))
    status = await gpu_driver_service.get_status("dra", cluster_id=None, hardware="intel-xpu")
    assert status.cluster_reachable is False
    assert status.ready is False


@pytest.mark.asyncio
async def test_install_dra_applies_nfd_then_dra_manifest(monkeypatch):
    runner = FakeRunner()
    _patch_runner(monkeypatch, runner)
    response = await gpu_driver_service.install("dra", cluster_id=None, hardware="intel-xpu")
    assert response.applied is True
    assert len(runner.applied) == 3
    assert "deployments/nfd?ref=" in runner.applied[0]
    assert "node-feature-rules" in runner.applied[1]
    assert "intel-resource-drivers-for-kubernetes/deployments/gpu" in runner.applied[2]


@pytest.mark.asyncio
async def test_install_plugin_applies_three_manifests_in_order(monkeypatch):
    runner = FakeRunner()
    _patch_runner(monkeypatch, runner)
    response = await gpu_driver_service.install("plugin", cluster_id=None, hardware="intel-xpu")
    assert response.applied is True
    assert len(runner.applied) == 3
    assert "deployments/nfd?ref=" in runner.applied[0]
    assert "node-feature-rules" in runner.applied[1]
    assert "gpu_plugin/overlays/nfd_labeled_nodes" in runner.applied[2]


@pytest.mark.asyncio
async def test_install_fails_fast_when_no_node_has_intel_gpu_hardware(monkeypatch):
    monkeypatch.setattr(gpu_driver_service, "_HARDWARE_CHECK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(gpu_driver_service, "_HARDWARE_CHECK_POLL_INTERVAL_SECONDS", 0.01)
    runner = FakeRunner(gpu_labelled_nodes=[])
    _patch_runner(monkeypatch, runner)
    with pytest.raises(gpu_driver_service.GpuDriverError) as excinfo:
        await gpu_driver_service.install("dra", cluster_id=None, hardware="intel-xpu")
    assert "No node in this cluster was detected with real Intel GPU hardware" in str(excinfo.value)
    # Only the NFD detection manifests were applied -- the driver/plugin
    # manifest itself must never be applied when no hardware is present.
    assert len(runner.applied) == 2


@pytest.mark.asyncio
async def test_install_raises_on_apply_failure(monkeypatch):
    class FailingRunner(FakeRunner):
        async def run(self, argv, timeout=10):
            command = " ".join(argv)
            if command.startswith("kubectl apply -k"):
                return _result(argv, stderr="boom", returncode=1)
            return await super().run(argv, timeout)

    _patch_runner(monkeypatch, FailingRunner())
    with pytest.raises(gpu_driver_service.GpuDriverError):
        await gpu_driver_service.install("dra", cluster_id=None, hardware="intel-xpu")


@pytest.mark.asyncio
async def test_install_wraps_timeout(monkeypatch):
    class TimingOutRunner(FakeRunner):
        async def run(self, argv, timeout=10):
            if " ".join(argv).startswith("kubectl apply -k"):
                raise CommandTimeoutError("timed out")
            return await super().run(argv, timeout)

    _patch_runner(monkeypatch, TimingOutRunner())
    with pytest.raises(gpu_driver_service.GpuDriverError):
        await gpu_driver_service.install("dra", cluster_id=None, hardware="intel-xpu")


def test_driver_provider_selection_by_hardware_id():
    from llm_d_bench.hardware.providers.intel_xpu import IntelXpuProvider
    from llm_d_bench.hardware.providers.nvidia import NvidiaProvider
    from llm_d_bench.hardware.registry import get_provider, register_provider

    if get_provider("intel-xpu") is None:
        register_provider(IntelXpuProvider())
    if get_provider("nvidia") is None:
        register_provider(NvidiaProvider())
    assert gpu_driver_service._driver_provider("intel-xpu").profile().id == "intel-xpu"
    assert gpu_driver_service._driver_provider("nvidia").profile().id == "nvidia"
    # No `hardware` with several driver providers is ambiguous, not a silent Intel default.
    with pytest.raises(gpu_driver_service.GpuDriverError):
        gpu_driver_service._driver_provider()
    with pytest.raises(gpu_driver_service.GpuDriverError):
        gpu_driver_service._driver_provider("does-not-exist")


@pytest.mark.asyncio
async def test_install_rejects_dra_on_old_kubernetes(monkeypatch):
    class OldRunner(FakeRunner):
        async def run(self, argv, timeout=10):
            if list(argv[:2]) == ["kubectl", "version"]:
                return _result(argv, json.dumps({"serverVersion": {"major": "1", "minor": "25"}}))
            return await super().run(argv, timeout)

    _patch_runner(monkeypatch, OldRunner())
    with pytest.raises(gpu_driver_service.GpuDriverError) as excinfo:
        await gpu_driver_service.install("dra", cluster_id=None, hardware="intel-xpu")
    assert "1.34.0" in str(excinfo.value)
    assert "v1.25" in str(excinfo.value)


def test_status_endpoint(monkeypatch):
    _patch_runner(monkeypatch, FakeRunner(daemonsets=[]))
    response = client.get(
        "/api/v1/monitoring/gpu-driver/status",
        params={"access_mode": "dra", "hardware": "intel-xpu"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_mode"] == "dra"
    assert body["installed"] is False


def test_install_endpoint(monkeypatch):
    _patch_runner(monkeypatch, FakeRunner())
    response = client.post(
        "/api/v1/monitoring/gpu-driver/install",
        json={"access_mode": "dra"},
        params={"hardware": "intel-xpu"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["applied"] is True
