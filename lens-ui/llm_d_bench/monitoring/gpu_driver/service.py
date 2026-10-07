"""Detect and install the Intel GPU DRA driver or Kubernetes device plugin.

Unlike ``llm_d_bench.monitoring.accelerator`` (which installs Intel's xpumd
*telemetry* chart on top of an already-present driver), this module installs
the driver/plugin itself -- the hardware-access prerequisite. It backs the
cluster-creation wizard's Accelerator step, which only needs "is a GPU driver
already usable on this cluster, and if not, install one" -- no telemetry, no
user-facing namespace (detection scans every namespace, so there is nothing
for the user to configure).

Deployed straight from the projects' own published kustomize manifests via
``kubectl apply -k`` (no Helm involved):
  * DRA driver: https://github.com/intel/intel-resource-drivers-for-kubernetes
  * device plugin: https://github.com/intel/intel-device-plugins-for-kubernetes

Both projects publish quarterly releases; the refs below are pinned to the
latest validated release as of this writing. Bump them when a newer release
has been validated.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from llm_d_bench.hardware.version import access_mode_version_message, server_kubernetes_version
from llm_d_bench.utils.kubernetes import cluster_info, list_resources, run_kubectl
from llm_d_bench.utils.shell import CommandTimeoutError

from .errors import GpuDriverError
from .models import GpuAccessMode, GpuDriverInstallResponse, GpuDriverStatusResponse

# Fallback "from sources" refs for the Intel GPU DRA driver / device plugin
# NFD install path. The effective values come from the registered Intel
# hardware profile (`driver` / `presence`); these literals apply only when
# hardware discovery is unavailable.
_DRA_RELEASE_REF = "gpu-v0.11.0"
_DEVICE_PLUGIN_RELEASE_REF = "v0.36.0"
_APPLY_TIMEOUT_SECONDS = 180.0
_LABEL = {"dra": "DRA driver", "plugin": "device plugin"}
_NFD_BASE = "https://github.com/intel/intel-device-plugins-for-kubernetes/deployments"


def _intel_profile():
    """Registered Intel XPU profile, or None when discovery is unavailable."""
    try:
        from llm_d_bench.hardware.registry import get_profile

        return get_profile("intel-xpu")
    except Exception:  # pragma: no cover - driver install must not fail on discovery errors
        return None


def _nfd_commands() -> list[list[str]]:
    profile = _intel_profile()
    driver = profile.driver if profile is not None else None
    refs = tuple(driver.nfd_manifest_refs) if driver is not None else ()
    if not refs:
        refs = (
            f"{_NFD_BASE}/nfd?ref={_DEVICE_PLUGIN_RELEASE_REF}",
            f"{_NFD_BASE}/nfd/overlays/node-feature-rules?ref={_DEVICE_PLUGIN_RELEASE_REF}",
        )
    return [["apply", "-k", ref] for ref in refs]


def _gpu_node_label_selector() -> str:
    profile = _intel_profile()
    presence = profile.presence if profile is not None else None
    selector = dict(presence.node_label_selector) if presence is not None else {}
    selector = selector or {"intel.feature.node.kubernetes.io/gpu": "true"}
    return ",".join(f"{key}={value}" for key, value in selector.items())


def _presence_timeouts() -> tuple[float, float]:
    profile = _intel_profile()
    presence = profile.presence if profile is not None else None
    if presence is None:
        return 90.0, 5.0
    return float(presence.timeout_seconds), float(presence.poll_interval_seconds)


# NFD + its GPU NodeFeatureRule are applied unconditionally before *either*
# access mode's own manifests -- they are the only reliable, upstream-vetted
# signal ("does real Intel GPU hardware exist on any node?") we have. The
# device plugin already depended on this for its `nodeSelector`; the DRA
# driver's DaemonSet has no such selector (it schedules everywhere and would
# otherwise just crash-loop/report no devices on GPU-less nodes), so it needs
# the same check performed independently.
#
# Effective values are profile-derived with literal fallback; kept as module
# attributes so tests can override them.
_NFD_COMMANDS: list[list[str]] = _nfd_commands()
_GPU_NODE_LABEL_SELECTOR = _gpu_node_label_selector()
# Bounded wait for NFD to label a node -- far shorter than the 5 minute
# DaemonSet-ready timeout the caller would otherwise sit through with zero
# chance of success when there is no GPU hardware at all.
_HARDWARE_CHECK_TIMEOUT_SECONDS, _HARDWARE_CHECK_POLL_INTERVAL_SECONDS = _presence_timeouts()


def _is_dra_driver(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    name = str(metadata.get("name") or "").lower()
    namespace = str(metadata.get("namespace") or "")
    labels = metadata.get("labels") or {}
    return (
        "intel-gpu-resource-driver" in name
        or namespace == "intel-gpu-resource-driver"
        or labels.get("app.kubernetes.io/name") == "intel-gpu-resource-driver"
    )


def _is_gpu_plugin(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    name = str(metadata.get("name") or "").lower()
    labels = metadata.get("labels") or {}
    return "intel-gpu-plugin" in name or labels.get("app") == "intel-gpu-plugin"


def _daemonset_ready(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    desired = int(status.get("desiredNumberScheduled", 0) or 0)
    ready = int(status.get("numberReady", 0) or 0)
    return desired > 0 and ready >= desired


def _driver_provider(hardware: str | None = None):
    """Provider that declares driver support, or None when none is registered.

    ``hardware`` is the optional ``hardware`` query parameter (a hardware
    profile id). When given, an unregistered/unsupported id is an error rather
    than a silent fall back to the built-in implementation.
    """
    try:
        from llm_d_bench.hardware.errors import HardwareRegistrationError
        from llm_d_bench.hardware.registry import get_driver_provider
    except Exception:  # pragma: no cover - fall back to the built-in implementation
        return None
    try:
        return get_driver_provider(hardware)
    except HardwareRegistrationError as error:
        # Several driver providers are registered and none was selected: require
        # an explicit `hardware` instead of silently installing the built-in one.
        raise GpuDriverError(str(error), status_code=409) from error
    except Exception as error:
        if hardware:
            raise GpuDriverError(str(error), status_code=404) from error
        return None


async def get_status(
    access_mode: GpuAccessMode, *, cluster_id: str | None, hardware: str | None = None
) -> GpuDriverStatusResponse:
    """Driver status, delegated to the provider that declares driver support."""
    provider = _driver_provider(hardware)
    if provider is not None:
        return await provider.driver_status(access_mode, cluster_id=cluster_id)
    return await _intel_driver_status(access_mode, cluster_id=cluster_id)


async def _intel_driver_status(access_mode: GpuAccessMode, *, cluster_id: str | None) -> GpuDriverStatusResponse:
    """Scan every namespace for a matching DaemonSet -- already-installed
    drivers are detected and reused regardless of which namespace they live
    in, so there is nothing for the caller to specify beyond the mode."""
    try:
        reachable = await cluster_info(cluster_id)
    except FileNotFoundError as error:
        raise GpuDriverError(str(error), status_code=400) from error
    if reachable.returncode != 0:
        return GpuDriverStatusResponse(
            access_mode=access_mode,
            installed=False,
            ready=False,
            cluster_reachable=False,
            message=reachable.stderr.strip() or "Kubernetes cluster is unreachable",
        )
    unsupported = access_mode_version_message(
        _intel_profile(), access_mode, await server_kubernetes_version(cluster_id)
    )
    if unsupported:
        return GpuDriverStatusResponse(
            access_mode=access_mode, installed=False, ready=False, cluster_reachable=True, message=unsupported
        )
    daemonsets = await list_resources("daemonsets", all_namespaces=True, cluster_id=cluster_id)
    matcher = _is_dra_driver if access_mode == "dra" else _is_gpu_plugin
    matched = [item for item in daemonsets if matcher(item)]
    label = _LABEL[access_mode]
    if not matched:
        return GpuDriverStatusResponse(
            access_mode=access_mode,
            installed=False,
            ready=False,
            cluster_reachable=True,
            message=f"Intel GPU {label} was not detected on this cluster.",
        )
    ready = any(_daemonset_ready(item) for item in matched)
    message = (
        f"Intel GPU {label} is installed and ready."
        if ready
        else f"Intel GPU {label} is installed but its pods are not all ready yet."
    )
    return GpuDriverStatusResponse(
        access_mode=access_mode, installed=True, ready=ready, cluster_reachable=True, message=message
    )


def _install_commands(access_mode: GpuAccessMode) -> list[list[str]]:
    profile = _intel_profile()
    driver = profile.driver if profile is not None else None
    mode = driver.mode(access_mode) if driver is not None else None
    if access_mode == "dra":
        ref = (mode.manifest_ref if mode is not None else None) or (
            "https://github.com/intel/intel-resource-drivers-for-kubernetes/deployments/gpu"
            f"?ref={_DRA_RELEASE_REF}"
        )
        return [["apply", "-k", ref]]
    ref = (mode.manifest_ref if mode is not None else None) or (
        f"{_NFD_BASE}/gpu_plugin/overlays/nfd_labeled_nodes?ref={_DEVICE_PLUGIN_RELEASE_REF}"
    )
    return [["apply", "-k", ref]]


async def _any_node_has_intel_gpu(cluster_id: str | None) -> bool:
    nodes = await list_resources("nodes", selector=_GPU_NODE_LABEL_SELECTOR, cluster_id=cluster_id)
    return bool(nodes)


async def _ensure_intel_gpu_hardware_detected(cluster_id: str | None) -> None:
    """Apply NFD + its GPU ``NodeFeatureRule`` and wait (bounded) for at
    least one node to be labelled as having real Intel GPU hardware.

    Both the device plugin and the DRA driver are useless without real
    hardware -- the plugin's DaemonSet would sit at ``0/0`` desired forever
    (its ``nodeSelector`` never matches), and the DRA driver's DaemonSet
    (which has no such selector) would schedule everywhere but its pods
    would report no devices / may crash-loop. Detecting this upfront lets
    callers fail in under two minutes instead of exhausting a five minute
    "wait for ready" poll with no chance of success.
    """
    for command in _NFD_COMMANDS:
        result = await run_kubectl(command, timeout=_APPLY_TIMEOUT_SECONDS, cluster_id=cluster_id)
        if result.returncode != 0:
            raise GpuDriverError(
                f"kubectl {' '.join(command)} failed: "
                f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
            )
    deadline = time.monotonic() + _HARDWARE_CHECK_TIMEOUT_SECONDS
    while True:
        if await _any_node_has_intel_gpu(cluster_id):
            return
        if time.monotonic() >= deadline:
            raise GpuDriverError(
                "No node in this cluster was detected with real Intel GPU hardware -- Node Feature "
                f"Discovery never labelled any node '{_GPU_NODE_LABEL_SELECTOR}' within "
                f"{_HARDWARE_CHECK_TIMEOUT_SECONDS:.0f}s. Installing the driver/plugin would leave it "
                "stuck at 0 ready pods indefinitely; skip the accelerator step for this cluster, or "
                "retry once real Intel GPU hardware is present.",
                status_code=422,
            )
        await asyncio.sleep(_HARDWARE_CHECK_POLL_INTERVAL_SECONDS)


async def install(
    access_mode: GpuAccessMode, *, cluster_id: str | None, hardware: str | None = None
) -> GpuDriverInstallResponse:
    """Install the driver, delegated to the provider that declares support."""
    provider = _driver_provider(hardware)
    if provider is not None:
        return await provider.install_driver(access_mode, cluster_id=cluster_id)
    return await _intel_install_driver(access_mode, cluster_id=cluster_id)


async def _intel_install_driver(access_mode: GpuAccessMode, *, cluster_id: str | None) -> GpuDriverInstallResponse:
    """Verify real Intel GPU hardware exists, then apply the upstream
    kustomize manifests for ``access_mode``.

    ``kubectl apply`` is declarative/idempotent, so this is safe to call even
    if some or all of the manifests are already present (e.g. a partially
    applied previous attempt, or a driver installed by other means that
    happens to share a manifest). Only applies the objects -- does not wait
    for pods to become Ready; callers should poll :func:`get_status`.
    """
    unsupported = access_mode_version_message(
        _intel_profile(), access_mode, await server_kubernetes_version(cluster_id)
    )
    if unsupported:
        raise GpuDriverError(unsupported, status_code=422)
    try:
        await _ensure_intel_gpu_hardware_detected(cluster_id)
        for command in _install_commands(access_mode):
            result = await run_kubectl(command, timeout=_APPLY_TIMEOUT_SECONDS, cluster_id=cluster_id)
            if result.returncode != 0:
                raise GpuDriverError(
                    f"kubectl {' '.join(command)} failed: "
                    f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
                )
    except FileNotFoundError as error:
        raise GpuDriverError(str(error), status_code=400) from error
    except CommandTimeoutError as error:
        raise GpuDriverError(
            f"Timed out applying Intel GPU {_LABEL[access_mode]} manifests: {error}",
            status_code=504,
        ) from error
    label = _LABEL[access_mode]
    return GpuDriverInstallResponse(
        access_mode=access_mode,
        applied=True,
        message=f"Applied the Intel GPU {label} manifests. It may take a few minutes for pods to become ready.",
    )
