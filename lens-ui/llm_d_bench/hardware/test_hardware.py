"""Tests for hardware profile registry, resolver, discovery and API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.hardware import discovery, registry
from llm_d_bench.hardware.errors import (
    HardwareProfileInvalidError,
    HardwareProfileNotFoundError,
    HardwareRegistrationError,
)
from llm_d_bench.hardware.models import HardwareProfile
from llm_d_bench.hardware.providers.intel_xpu import IntelXpuProvider
from llm_d_bench.hardware.registry import (
    all_profiles,
    get_profile,
    get_provider,
    register_profile,
    register_provider,
)
from llm_d_bench.hardware.resolver import (
    require_accelerator,
    resolve_by_accelerator_key,
    resolve_by_aic_system,
    resolve_by_device_class,
    resolve_by_node_label,
    resolve_by_resource,
    resolve_by_upstream_variant,
)


@pytest.fixture(autouse=True)
def _reset_hardware_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


def _payload(**overrides) -> dict:
    payload = {
        "id": "fake",
        "display_name": "Fake GPU",
        "vendor": "fake",
        "request_model": "dra",
        "deployment": {"overlay_root": "guides/{guide}/modelserver/fake/vllm", "arch": "fake"},
    }
    payload.update(overrides)
    return payload


def test_bundled_intel_profile_matches_legacy_constants():
    profile = get_profile("intel-xpu")
    assert profile.request_model == "dra"
    assert profile.device_classes == ("gpu.intel.com",)
    assert profile.resource_prefixes == ("gpu.intel.com/",)
    assert profile.monitor_resource_suffixes == ("monitoring",)
    assert profile.node_label_selector == {"intel.feature.node.kubernetes.io/gpu": "true"}
    assert profile.accelerator_keys == ("xpu", "intel_gpu")
    assert profile.benchmark_profile == "intel-xpu"
    assert profile.evaluation_profile == "intel-xpu"
    assert profile.upstream_variant == "xpu"
    assert profile.upstream_vendor == "intel"
    assert profile.deployment.device_class == "gpu.intel.com"
    assert profile.deployment.claim_request_name == "intel"
    assert profile.deployment.arch == "xpu"
    assert profile.driver is not None
    assert profile.driver.installer == "kustomize"
    dra = profile.driver.mode("dra")
    plugin = profile.driver.mode("plugin")
    assert dra is not None and plugin is not None
    assert dra.manifest_ref.endswith("ref=gpu-v0.11.0")
    assert plugin.manifest_ref.endswith("ref=v0.36.0")
    assert plugin.monitoring_flag == "-enable-monitoring"
    assert profile.presence is not None
    assert profile.presence.timeout_seconds == 90
    assert profile.presence.poll_interval_seconds == 5
    assert profile.telemetry is not None
    assert profile.telemetry.provider_id == "intel_gpu"
    assert profile.telemetry.allocation_join == "dra"
    assert profile.telemetry.device_metrics["vram"] == "hw_memory_size_bytes"
    assert profile.telemetry.label_schema["pci"] == "pci_bdf"


def test_bundled_payloads_validate_against_schema():
    for payload in discovery.bundled_profile_payloads():
        discovery.validate_profile_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "x"},
        _payload(request_model="cuda"),
        {**_payload(), "unknown_field": 1},
        _payload(driver={"installer": "apt"}),
    ],
)
def test_invalid_profile_payloads_are_rejected(payload):
    with pytest.raises(HardwareProfileInvalidError):
        discovery.validate_profile_payload(payload)


def test_resolvers_resolve_intel_from_each_identity():
    assert resolve_by_device_class("gpu.intel.com").id == "intel-xpu"
    assert resolve_by_resource("gpu.intel.com/xe").id == "intel-xpu"
    assert resolve_by_node_label({"intel.feature.node.kubernetes.io/gpu": "true"}).id == "intel-xpu"
    assert resolve_by_upstream_variant("xpu").id == "intel-xpu"
    assert resolve_by_upstream_variant("xpu", "intel").id == "intel-xpu"
    assert resolve_by_accelerator_key("xpu").id == "intel-xpu"
    assert resolve_by_accelerator_key("intel_gpu").id == "intel-xpu"
    assert resolve_by_aic_system("BMG 1550").id == "intel-xpu"


def test_unregistered_identity_returns_none_and_require_raises():
    assert resolve_by_device_class("gpu.example.com") is None
    assert resolve_by_accelerator_key("does-not-exist") is None
    assert resolve_by_node_label({"example.com/gpu.present": "true"}) is None
    with pytest.raises(HardwareProfileNotFoundError):
        require_accelerator("does-not-exist")


def test_known_hardware_resolves_for_intel_and_nvidia():
    assert resolve_by_accelerator_key("xpu").id == "intel-xpu"
    assert resolve_by_accelerator_key("cuda").id == "nvidia"
    assert resolve_by_device_class("gpu.nvidia.com").id == "nvidia"
    assert resolve_by_node_label({"feature.node.kubernetes.io/pci-10de.present": "true"}).id == "nvidia"
    assert resolve_by_resource("nvidia.com/gpu").id == "nvidia"
    assert resolve_by_upstream_variant("gpu", "nvidia").id == "nvidia"


def test_conflicting_identity_registration_raises():
    all_profiles()  # ensure bundled profiles are loaded first
    with pytest.raises(HardwareRegistrationError):
        register_profile(HardwareProfile.from_dict(_payload(device_classes=["gpu.intel.com"])))
    with pytest.raises(HardwareRegistrationError):
        register_profile(HardwareProfile.from_dict(_payload(accelerator_keys=["xpu"])))


def test_builtin_provider_exposes_intel_profile():
    assert IntelXpuProvider().profile().id == "intel-xpu"


def test_provider_registry_stores_provider_under_profile_id():
    class FakeProvider:
        def profile(self) -> HardwareProfile:
            return HardwareProfile.from_dict(_payload())

    register_provider(FakeProvider())
    provider = get_provider("fake")
    assert provider is not None
    assert provider.profile().id == "fake"


def test_driver_provider_selects_by_registered_hardware_id():
    from llm_d_bench.hardware.providers.nvidia import NvidiaProvider
    from llm_d_bench.hardware.registry import get_driver_provider

    if get_provider("intel-xpu") is None:
        register_provider(IntelXpuProvider())
    if get_provider("nvidia") is None:
        register_provider(NvidiaProvider())
    # Two driver providers are registered, so the unspecified default is ambiguous.
    with pytest.raises(HardwareRegistrationError):
        get_driver_provider()
    assert get_driver_provider("intel-xpu").profile().id == "intel-xpu"
    assert get_driver_provider("nvidia").profile().id == "nvidia"
    with pytest.raises(HardwareProfileNotFoundError):
        get_driver_provider("does-not-exist")


def test_capabilities_endpoint_lists_intel_profile():
    response = TestClient(app).get("/api/v1/hardware/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["version"]
    by_id = {profile["id"]: profile for profile in body["profiles"]}
    assert "intel-xpu" in by_id
    assert by_id["intel-xpu"]["deployment"]["arch"] == "xpu"


def test_capabilities_version_is_stable_across_calls():
    first = TestClient(app).get("/api/v1/hardware/capabilities").json()["version"]
    second = TestClient(app).get("/api/v1/hardware/capabilities").json()["version"]
    assert first == second
