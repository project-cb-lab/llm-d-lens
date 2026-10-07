"""The benchmark runtime profile is resolved from the hardware profile registry."""

from __future__ import annotations

import json
from types import SimpleNamespace

from llm_d_bench.evaluate.router import _execution_accelerator_profile


def _execution(manifest: str, image: str = "") -> SimpleNamespace:
    artifact = SimpleNamespace(
        content=json.dumps({"officialGuide": {"renderedManifest": manifest}, "runtime": {"image": image}}),
        provider_ref="",
    )
    return SimpleNamespace(configuration_artifacts=[artifact])


def test_resolves_intel_device_class_to_profile_benchmark_name():
    execution = _execution("spec:\n  deviceClassName: gpu.intel.com\n")
    assert _execution_accelerator_profile(execution) == "intel-xpu"


def test_configured_profile_wins():
    assert _execution_accelerator_profile(_execution(""), "custom-profile") == "custom-profile"


def test_unknown_device_class_has_no_profile():
    assert _execution_accelerator_profile(_execution("  deviceClassName: example.com\n")) is None


def test_xpu_runtime_falls_back_to_intel_profile():
    execution = _execution("", image="ghcr.io/llm-d/llm-d-xpu:v0.9.0")
    assert _execution_accelerator_profile(execution) == "intel-xpu"
