"""NVIDIA GPU hardware provider.

Declares the NVIDIA profile and implements the provider contract: a read-only
presence probe plus Helm-based device-plugin / DRA-driver status and install.
The host kernel driver is assumed to be pre-installed (decision D1); Lens only
installs the Kubernetes device plugin or DRA driver.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from llm_d_bench.utils import kubernetes as k8s
from llm_d_bench.utils.shell import CommandTimeoutError

from ..models import HardwareProfile
from ..registry import get_profile
from ..version import access_mode_version_message, server_kubernetes_version

if TYPE_CHECKING:
    from llm_d_bench.monitoring.gpu_driver.models import GpuDriverInstallResponse, GpuDriverStatusResponse

# NOTE: monitoring.gpu_driver is imported lazily inside the methods below. Its
# package import reads profile configuration at import time, which would create
# a cycle while this provider itself is being loaded by entry-point discovery.

_RELEASE_NAME = "nvidia-device-plugin"
_DRA_RELEASE_NAME = "nvidia-dra-driver-gpu"
_DEFAULT_NAMESPACE = "kube-system"
_DRA_NAMESPACE = "nvidia-dra-driver-gpu"
_APPLY_TIMEOUT_SECONDS = 180.0
_HARDWARE_CHECK_TIMEOUT_SECONDS = 90.0
_HARDWARE_CHECK_POLL_INTERVAL_SECONDS = 5.0
_LABEL = {"dra": "DRA driver", "plugin": "device plugin"}


def load_profiles() -> list[dict[str, Any]]:
    """Return the bundled NVIDIA profile payload(s)."""
    text = files("llm_d_bench.hardware").joinpath("profiles", "nvidia.json").read_text(encoding="utf-8")
    return [json.loads(text)]


def profile() -> HardwareProfile:
    return get_profile("nvidia")


def _matches(item: Mapping[str, Any], matchers: Sequence[Mapping[str, str]]) -> bool:
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

    return any(all(condition(key, str(value)) for key, value in matcher.items()) for matcher in matchers)


def _daemonset_ready(item: Mapping[str, Any]) -> bool:
    status = item.get("status") or {}
    desired = int(status.get("desiredNumberScheduled", 0) or 0)
    ready = int(status.get("numberReady", 0) or 0)
    return desired > 0 and ready >= desired


async def hardware_present(cluster_id: str | None) -> bool:
    """Read-only presence probe: node label, DRA ResourceSlice or extended resource."""
    hardware = profile()
    if hardware.node_label_selector:
        selector = ",".join(f"{key}={value}" for key, value in hardware.node_label_selector.items())
        if await k8s.list_resources("nodes", selector=selector, cluster_id=cluster_id):
            return True
    try:
        slices = await k8s.list_resources("resourceslices", cluster_id=cluster_id)
    except Exception:  # pragma: no cover - optional DRA CRD
        slices = []
    device_classes = set(hardware.device_classes)
    if any(str((item.get("spec") or {}).get("driver") or "") in device_classes for item in slices):
        return True
    prefixes = tuple(hardware.resource_prefixes)
    suffixes = tuple(hardware.monitor_resource_suffixes)
    for node in await k8s.list_resources("nodes", cluster_id=cluster_id):
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        for key in allocatable:
            if any(str(key).startswith(prefix) for prefix in prefixes) and not any(
                str(key).endswith(f"/{suffix}") for suffix in suffixes
            ):
                return True
    return False


class NvidiaProvider:
    """NVIDIA GPU provider: profile link, presence probe and Helm driver install."""

    def profile(self) -> HardwareProfile:
        return profile()

    async def ensure_presence(self, *, cluster_id: str | None) -> None:
        from llm_d_bench.monitoring.gpu_driver.errors import GpuDriverError

        deadline = time.monotonic() + _HARDWARE_CHECK_TIMEOUT_SECONDS
        while True:
            if await hardware_present(cluster_id):
                return
            if time.monotonic() >= deadline:
                raise GpuDriverError(
                    "No NVIDIA GPU hardware detected (no node label, DRA ResourceSlice or nvidia.com/gpu "
                    f"extended resource) within {_HARDWARE_CHECK_TIMEOUT_SECONDS:.0f}s; install the host "
                    "driver first.",
                    status_code=422,
                )
            await asyncio.sleep(_HARDWARE_CHECK_POLL_INTERVAL_SECONDS)

    async def driver_status(self, access_mode: str, *, cluster_id: str | None) -> GpuDriverStatusResponse:
        from llm_d_bench.monitoring.gpu_driver.errors import GpuDriverError
        from llm_d_bench.monitoring.gpu_driver.models import GpuDriverStatusResponse

        driver = profile().driver
        mode = driver.mode(access_mode)
        matchers = mode.daemonset_matchers if mode is not None else ()
        label = _LABEL.get(access_mode, "driver")
        try:
            await k8s.cluster_info(cluster_id)
        except FileNotFoundError as error:
            raise GpuDriverError(str(error), status_code=400) from error
        unsupported = access_mode_version_message(profile(), access_mode, await server_kubernetes_version(cluster_id))
        if unsupported:
            return GpuDriverStatusResponse(
                access_mode=access_mode, installed=False, ready=False, cluster_reachable=True, message=unsupported
            )
        daemonsets = await k8s.list_resources("daemonsets", all_namespaces=True, cluster_id=cluster_id)
        matched = [item for item in daemonsets if _matches(item, matchers)]
        if not matched:
            return GpuDriverStatusResponse(
                access_mode=access_mode,
                installed=False,
                ready=False,
                cluster_reachable=True,
                message=f"NVIDIA GPU {label} was not detected on this cluster.",
            )
        ready = any(_daemonset_ready(item) for item in matched)
        return GpuDriverStatusResponse(
            access_mode=access_mode,
            installed=True,
            ready=ready,
            cluster_reachable=True,
            message=(
                f"NVIDIA GPU {label} is installed and ready."
                if ready
                else (
                    f"NVIDIA GPU {label} is installed but its pods are not all ready yet. Ensure the host "
                    "NVIDIA driver and the NVIDIA container toolkit (nvidia-container-toolkit) are installed "
                    "on the GPU nodes."
                )
            ),
        )

    async def install_driver(self, access_mode: str, *, cluster_id: str | None) -> GpuDriverInstallResponse:
        from llm_d_bench.monitoring.gpu_driver.errors import GpuDriverError
        from llm_d_bench.monitoring.gpu_driver.models import GpuDriverInstallResponse

        driver = profile().driver
        if driver.installer != "helm":
            raise GpuDriverError(f"unsupported installer {driver.installer!r} for NVIDIA")
        unsupported = access_mode_version_message(profile(), access_mode, await server_kubernetes_version(cluster_id))
        if unsupported:
            raise GpuDriverError(unsupported, status_code=422)
        mode = driver.mode(access_mode)
        if mode is None:
            raise GpuDriverError(f"NVIDIA {access_mode} access mode is not configured")
        if access_mode == "dra":
            namespace, release = _DRA_NAMESPACE, _DRA_RELEASE_NAME
        else:
            namespace, release = _DEFAULT_NAMESPACE, _RELEASE_NAME
        ref = mode.manifest_ref
        version = mode.chart_version
        repo = mode.repo
        if not ref:
            raise GpuDriverError(f"NVIDIA {access_mode} install reference is not configured")
        # Do NOT block on a Kubernetes presence probe here: the device plugin is
        # what publishes the `nvidia.com/gpu` extended resource (and NFD/GFD the
        # node labels), so before installing it that signal is legitimately
        # absent and waiting for it would time out. Readiness polling after the
        # install (gpu-driver status) reports a cluster without real hardware.
        argv = ["helm", "upgrade", "--install", release, ref, "--namespace", namespace, "--create-namespace"]
        if repo:
            argv += ["--repo", repo]
        if version:
            argv += ["--version", version]
        # Vendor-specific install options (each provider interprets its own keys).
        # nfd_enabled installs Node Feature Discovery alongside the plugin; the
        # plugin then schedules onto the NFD-labeled GPU nodes (the chart default
        # affinity). gfd_enabled adds GPU Feature Discovery, which writes the
        # nvidia.com/gpu.* node labels (product/family/count). device_plugin_all_nodes
        # instead clears that affinity and runs the plugin everywhere.
        # runtime_class_name pins the pod to the cluster's NVIDIA RuntimeClass,
        # without which the plugin container cannot see NVML and fails with
        # "invalid device discovery strategy".
        options = dict(mode.options or {})
        if options.get("nfd_enabled"):
            argv += ["--set", "nfd.enabled=true"]
        if options.get("gfd_enabled"):
            argv += ["--set", "gfd.enabled=true"]
        if options.get("device_plugin_all_nodes"):
            argv += ["--set", "failOnInitError=false", "--set", "affinity=null"]
        runtime_class = options.get("runtime_class_name")
        if runtime_class:
            argv += ["--set", f"runtimeClassName={runtime_class}"]
        try:
            result = await k8s.scoped_runner(cluster_id).run(argv, timeout=_APPLY_TIMEOUT_SECONDS)
        except (CommandTimeoutError, TimeoutError) as error:
            raise GpuDriverError(
                f"NVIDIA {access_mode} install timed out after {_APPLY_TIMEOUT_SECONDS:.0f}s "
                f"(check the host's network access to {repo or ref}): {error}",
                status_code=504,
            ) from error
        except FileNotFoundError as error:
            raise GpuDriverError(str(error), status_code=400) from error
        if result.returncode != 0:
            raise GpuDriverError(
                f"helm upgrade --install {release} failed: "
                f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
            )
        label = _LABEL.get(access_mode, "driver")
        return GpuDriverInstallResponse(
            access_mode=access_mode,
            applied=True,
            message=(
                f"Applied the NVIDIA GPU {label} Helm release. It may take a few minutes for pods to become ready."
            ),
        )
