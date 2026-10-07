"""Hardware identity helpers resolve from the registered profile."""

from __future__ import annotations

import pytest

from llm_d_bench.deploy.providers import hardware_profile


def test_defaults_match_the_registered_intel_profile():
    assert hardware_profile.device_class() == "gpu.intel.com"
    assert hardware_profile.claim_request_name() == "intel"
    assert hardware_profile.overlay_variant() == "xpu"
    assert hardware_profile.request_model() == "dra"
    assert hardware_profile.requires_dra_claim() is True
    assert hardware_profile.resource_name() is None


def test_accelerator_supported_uses_the_registry():
    assert hardware_profile.accelerator_supported("xpu") is True
    assert hardware_profile.accelerator_supported("cuda") is True
    assert hardware_profile.accelerator_supported("does-not-exist") is False
    assert hardware_profile.accelerator_supported(None) is False


def test_explicit_accelerator_overrides_the_environment_variable_default(monkeypatch):
    """A per-run accelerator always wins, regardless of PRISM_DEPLOY_ACCELERATOR.

    This is the contract the Deploy runtime relies on: the UI's hardware
    selection (threaded in explicitly) must never be shadowed by whatever a
    shared process environment variable happens to carry.
    """
    monkeypatch.setenv("PRISM_DEPLOY_ACCELERATOR", "xpu")
    assert hardware_profile.overlay_variant(accelerator="gpu") == "gpu"
    assert hardware_profile.device_class(accelerator="gpu") != hardware_profile.device_class(accelerator="xpu")
    assert hardware_profile.overlay_variant() == "xpu"  # no explicit accelerator still honors the env var/default


def test_explicit_accelerator_falls_back_to_environment_then_the_intel_default(monkeypatch):
    monkeypatch.delenv("PRISM_DEPLOY_ACCELERATOR", raising=False)
    assert hardware_profile.overlay_variant() == "xpu"
    monkeypatch.setenv("PRISM_DEPLOY_ACCELERATOR", "gpu")
    assert hardware_profile.overlay_variant() == "gpu"
    assert hardware_profile.overlay_variant(accelerator="xpu") == "xpu"


def test_set_accelerator_request_writes_the_dra_claim_count():
    claim = {"spec": {"spec": {"devices": {"requests": [{"exactly": {"deviceClassName": "gpu.intel.com"}}]}}}}
    container: dict = {}
    hardware_profile.set_accelerator_request(container, claim, 4)
    assert claim["spec"]["spec"]["devices"]["requests"][0]["exactly"]["count"] == 4
    assert "resources" not in container


def test_set_accelerator_request_writes_an_extended_resource(monkeypatch):
    monkeypatch.setattr(hardware_profile, "resource_name", lambda fallback=None, accelerator=None: "nvidia.com/gpu")
    container = {"resources": {"limits": {"cpu": "1"}}}
    hardware_profile.set_accelerator_request(container, None, 2)
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "2"
    assert container["resources"]["requests"]["nvidia.com/gpu"] == "2"
    assert container["resources"]["limits"]["cpu"] == "1"


def test_set_accelerator_request_without_claim_or_resource_name_raises(monkeypatch):
    monkeypatch.setattr(hardware_profile, "resource_name", lambda fallback=None, accelerator=None: None)
    with pytest.raises(ValueError):
        hardware_profile.set_accelerator_request({}, None, 1)
