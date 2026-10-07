"""NVIDIA provider: profile, presence probe and Helm driver wiring."""

from __future__ import annotations

import json

import pytest

from llm_d_bench.hardware import discovery
from llm_d_bench.hardware.providers import nvidia
from llm_d_bench.monitoring.gpu_driver.errors import GpuDriverError
from llm_d_bench.utils.shell import CommandResult


def _patch_scoped_runner(monkeypatch, *, version="1.32", record=None, deny_other=False):
    major, minor = (version.split(".") + ["0", "0"])[:2]

    class Runner:
        async def run(self, argv, timeout=10):
            del timeout
            if list(argv[:2]) == ["kubectl", "version"]:
                return CommandResult(
                    tuple(argv), 0, json.dumps({"serverVersion": {"major": major, "minor": minor}}), ""
                )
            if record is not None:
                record["argv"] = list(argv)
            if deny_other:
                raise AssertionError("helm must not run when the access mode is unsupported")
            return CommandResult(tuple(argv), 0, "ok", "")

    monkeypatch.setattr(nvidia.k8s, "scoped_runner", lambda cluster_id=None: Runner())


def test_bundled_nvidia_profile_validates():
    payloads = nvidia.load_profiles()
    assert len(payloads) == 1
    assert payloads[0]["id"] == "nvidia"
    assert payloads[0]["request_model"] == "extended-resource"
    discovery.validate_profile_payload(payloads[0])


def test_daemonset_matchers_match_name_label_and_namespace():
    assert nvidia._matches(
        {"metadata": {"name": "nvidia-device-plugin-daemonset"}},
        [{"name_contains": "nvidia-device-plugin"}],
    )
    assert nvidia._matches(
        {"metadata": {"name": "x", "labels": {"app.kubernetes.io/name": "nvidia-device-plugin"}}},
        [{"label": "app.kubernetes.io/name=nvidia-device-plugin"}],
    )
    assert nvidia._matches(
        {"metadata": {"name": "x", "namespace": "nvidia-dra-driver-gpu"}},
        [{"namespace": "nvidia-dra-driver-gpu"}],
    )
    assert not nvidia._matches({"metadata": {"name": "other"}}, [{"name_contains": "nvidia-device-plugin"}])


@pytest.mark.asyncio
async def test_driver_status_reports_ready(monkeypatch):
    async def fake_cluster_info(_cluster_id):
        return True

    async def fake_list_resources(resource, **_kwargs):
        if resource == "daemonsets":
            return [
                {
                    "metadata": {"name": "nvidia-device-plugin-daemonset"},
                    "status": {"desiredNumberScheduled": 1, "numberReady": 1},
                }
            ]
        return []

    monkeypatch.setattr(nvidia.k8s, "cluster_info", fake_cluster_info)
    monkeypatch.setattr(nvidia.k8s, "list_resources", fake_list_resources)
    _patch_scoped_runner(monkeypatch)
    status = await nvidia.NvidiaProvider().driver_status("plugin", cluster_id=None)
    assert status.installed is True
    assert status.ready is True


@pytest.mark.asyncio
async def test_install_driver_runs_the_pinned_helm_chart(monkeypatch):
    calls: dict[str, list[str]] = {}
    _patch_scoped_runner(monkeypatch, record=calls)
    response = await nvidia.NvidiaProvider().install_driver("plugin", cluster_id=None)
    assert response.applied is True
    argv = calls["argv"]
    assert argv[:3] == ["helm", "upgrade", "--install"]
    assert "nvidia-device-plugin" in argv
    assert "https://nvidia.github.io/k8s-device-plugin" in argv
    assert "0.17.4" in argv
    # The NVIDIA profile enables NFD, so the chart installs it and the plugin
    # schedules onto the NFD-labeled GPU nodes (no all-nodes override).
    assert "nfd.enabled=true" in argv
    assert "gfd.enabled=true" in argv
    assert "runtimeClassName=nvidia" in argv
    assert "failOnInitError=false" not in argv
    assert "affinity=null" not in argv


@pytest.mark.asyncio
async def test_install_driver_rejects_dra_on_old_kubernetes(monkeypatch):
    _patch_scoped_runner(monkeypatch, version="1.25", deny_other=True)
    with pytest.raises(GpuDriverError) as excinfo:
        await nvidia.NvidiaProvider().install_driver("dra", cluster_id=None)
    message = str(excinfo.value)
    assert "1.32.0" in message
    assert "v1.25" in message


@pytest.mark.asyncio
async def test_driver_status_flags_unsupported_dra_on_old_kubernetes(monkeypatch):
    async def fake_cluster_info(_cluster_id):
        return True

    async def fake_list_resources(_resource, **_kwargs):
        return []

    monkeypatch.setattr(nvidia.k8s, "cluster_info", fake_cluster_info)
    monkeypatch.setattr(nvidia.k8s, "list_resources", fake_list_resources)
    _patch_scoped_runner(monkeypatch, version="1.25")
    status = await nvidia.NvidiaProvider().driver_status("dra", cluster_id=None)
    assert status.installed is False
    assert "1.32.0" in status.message


@pytest.mark.asyncio
async def test_ensure_presence_fails_fast_without_hardware(monkeypatch):
    monkeypatch.setattr(nvidia, "_HARDWARE_CHECK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(nvidia, "_HARDWARE_CHECK_POLL_INTERVAL_SECONDS", 0.005)

    async def fake_list_resources(_resource, **_kwargs):
        return []

    monkeypatch.setattr(nvidia.k8s, "list_resources", fake_list_resources)
    with pytest.raises(GpuDriverError):
        await nvidia.NvidiaProvider().ensure_presence(cluster_id=None)
