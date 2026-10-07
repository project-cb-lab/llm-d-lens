"""Tests for the NVIDIA GPU (DCGM exporter) accelerator observability provider."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from llm_d_bench.monitoring.accelerator import nvidia_gpu
from llm_d_bench.monitoring.accelerator.models import AcceleratorInstallRequest
from llm_d_bench.monitoring.cluster_stack.models import (
    ClusterStackStatusResponse,
    ClusterSummary,
    HelmReleaseSummary,
)
from llm_d_bench.utils.shell import CommandResult


def _result(argv, stdout="", stderr="", returncode=0):
    return CommandResult(tuple(argv), returncode, stdout, stderr)


def _patch(monkeypatch, *, installed=False, gpu_nodes=True, plugin_ready=False, dra_ready=False):
    async def current_context(_cluster_id):
        return "ctx"

    async def cluster_info(_cluster_id):
        return _result(["kubectl", "cluster-info"], "running")

    async def helm_status(*_args, **_kwargs):
        if installed:
            return _result(["helm", "status"], json.dumps({"version": 2, "info": {"status": "deployed"}}))
        return _result(["helm", "status"], stderr="Error: release: not found", returncode=1)

    async def helm_list(*_args, **_kwargs):
        return [{"chart": "dcgm-exporter-4.8.4"}] if installed else []

    async def helm_get_values(*_args, **_kwargs):
        return {}

    async def list_resources(resource, **kwargs):
        if resource == "daemonsets" and kwargs.get("all_namespaces"):
            items = []
            if plugin_ready:
                items.append({"kind": "DaemonSet", "metadata": {"name": "nvidia-device-plugin-daemonset"},
                              "status": {"desiredNumberScheduled": 1, "numberReady": 1}})
            if dra_ready:
                items.append({"kind": "DaemonSet", "metadata": {"name": "nvidia-dra-driver-kubelet-plugin"},
                              "status": {"desiredNumberScheduled": 1, "numberReady": 1}})
            return items
        if resource == "nodes" and kwargs.get("selector"):
            return [{"metadata": {"name": "gpu-node-1"}}] if gpu_nodes else []
        if resource == "nodes":
            if not gpu_nodes:
                return []
            return [{"metadata": {"name": "gpu-node-1"}, "status": {"allocatable": {"nvidia.com/gpu": "8"}}}]
        if resource == "daemonsets,deployments":
            return [{"kind": "DaemonSet", "metadata": {"name": "dcgm-exporter"},
                     "status": {"desiredNumberScheduled": 1, "numberReady": 1}}] if installed else []
        if resource == "servicemonitors":
            return [{"metadata": {"name": "dcgm-exporter"}}] if installed else []
        if resource == "configmaps":
            return [{"metadata": {"name": "dcgm-exporter-dashboard"}}] if installed else []
        return []

    monkeypatch.setattr(nvidia_gpu.k8s, "current_context", current_context)
    monkeypatch.setattr(nvidia_gpu.k8s, "cluster_info", cluster_info)
    monkeypatch.setattr(nvidia_gpu.k8s, "helm_status", helm_status)
    monkeypatch.setattr(nvidia_gpu.k8s, "helm_list", helm_list)
    monkeypatch.setattr(nvidia_gpu.k8s, "helm_get_values", helm_get_values)
    monkeypatch.setattr(nvidia_gpu.k8s, "list_resources", list_resources)


def test_capability_identifies_nvidia_gpu():
    capability = nvidia_gpu.NvidiaGpuProvider().capability()
    assert capability.type == "nvidia_gpu"
    assert capability.display_name == "NVIDIA GPU"
    assert capability.access_modes == ["plugin", "dra"]
    assert capability.release_name == "dcgm-exporter"


def test_dashboard_manifest_is_a_grafana_configmap():
    from llm_d_bench.monitoring.accelerator.dcgm_dashboard import DASHBOARD_NAME, dashboard_manifest

    manifest = dashboard_manifest("llm-d-monitoring")
    assert manifest["kind"] == "ConfigMap"
    assert manifest["metadata"]["name"] == DASHBOARD_NAME
    assert manifest["metadata"]["namespace"] == "llm-d-monitoring"
    assert manifest["metadata"]["labels"]["grafana_dashboard"] == "1"
    dashboard = json.loads(manifest["data"]["dashboard.json"])
    assert dashboard["title"] == "NVIDIA DCGM Exporter"
    assert {panel["title"] for panel in dashboard["panels"]} >= {"GPU utilization", "GPU memory used"}


def test_install_argv_pins_the_dcgm_exporter_chart():
    argv = nvidia_gpu.NvidiaGpuProvider().install_argv(
        AcceleratorInstallRequest(accelerator="nvidia_gpu", namespace="dcgm-exporter"),
        monitoring_namespace="llm-d-monitoring",
        monitoring_release="llmd",
    )
    assert argv[:4] == ["helm", "upgrade", "--install", "dcgm-exporter"]
    assert nvidia_gpu._CHART_REPO in argv
    assert "4.8.4" in argv
    assert "runtimeClassName=nvidia" in argv
    assert "intel-xpumd" not in argv


@pytest.mark.asyncio
async def test_discover_status_reports_absent(monkeypatch):
    _patch(monkeypatch, installed=False)
    status = await nvidia_gpu.NvidiaGpuProvider().discover_status("dcgm-exporter", None)
    assert status.accelerator == "nvidia_gpu"
    assert status.status == "absent"
    assert status.cluster_reachable is True


@pytest.mark.asyncio
async def test_discover_status_reports_ready(monkeypatch):
    _patch(monkeypatch, installed=True, plugin_ready=True)
    status = await nvidia_gpu.NvidiaGpuProvider().discover_status("dcgm-exporter", None)
    assert status.status == "ready"
    assert {mode.mode: mode.available for mode in status.access_modes}["plugin"] is True


@pytest.mark.asyncio
async def test_preflight_blocks_without_nvidia_hardware(monkeypatch):
    _patch(monkeypatch, installed=False, gpu_nodes=False)
    monkeypatch.setattr(nvidia_gpu, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(nvidia_gpu, "hardware_present", _always(False))
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    result = await nvidia_gpu.NvidiaGpuProvider().preflight(
        AcceleratorInstallRequest(accelerator="nvidia_gpu"), None, monitoring_status=monitoring
    )
    checks = {check.name: check for check in result.checks}
    assert checks["gpu_nodes"].blocking is True
    assert checks["gpu_nodes"].passed is False
    assert result.allowed is False


@pytest.mark.asyncio
async def test_preflight_allows_when_hardware_and_monitoring_ready(monkeypatch):
    _patch(monkeypatch, installed=False, gpu_nodes=True, plugin_ready=True)
    monkeypatch.setattr(nvidia_gpu, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(nvidia_gpu, "hardware_present", _always(True))
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    result = await nvidia_gpu.NvidiaGpuProvider().preflight(
        AcceleratorInstallRequest(accelerator="nvidia_gpu", access_mode="plugin", namespace="dcgm-exporter"),
        None,
        monitoring_status=monitoring,
    )
    assert result.allowed is True
    assert result.command_preview[0] == "helm"


def _always(value):
    async def probe(_cluster_id):
        return value

    return probe
