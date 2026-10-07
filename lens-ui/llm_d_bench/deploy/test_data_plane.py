"""Coverage for generic deployment data-plane selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_d_bench.deploy.data_plane import (
    GATEWAY_MODE_VALUES_PATH,
    PLAINTEXT_EPP_VALUES_PATH,
    declared_data_plane_kind,
    deployment_uses_shared_gateway,
    resolve_data_plane,
    router_data_plane_args,
    router_data_plane_effective_values,
)


def _execution(provenance=None, contract=None, provider_ref=None, data_plane=None):
    value = {"deployment_contract": contract or {}, "provider_ref": provider_ref}
    return SimpleNamespace(
        provenance=provenance or {},
        artifact=SimpleNamespace(rendered_payload=SimpleNamespace(value=value)),
        data_plane=data_plane,
    )


def test_declared_kind_reads_the_explicit_data_plane_kind():
    assert declared_data_plane_kind(_execution(contract={"data_plane_kind": "llm-d-router"})) == "llm-d-router"
    assert declared_data_plane_kind(_execution(contract={"data_plane_kind": "direct"})) == "direct"
    assert declared_data_plane_kind(_execution(contract={"data_plane_kind": "external"})) == "external"


def test_declared_kind_falls_back_for_legacy_executions():
    # Contracts predating data_plane_kind only carried shares_gateway.
    assert declared_data_plane_kind(_execution(contract={"shares_gateway": True})) == "llm-d-router"
    # Older still: only the provider ref, and optimized-baseline was the only guide.
    assert declared_data_plane_kind(_execution(provider_ref="optimized-baseline")) == "llm-d-router"
    assert declared_data_plane_kind(_execution()) == "external"


def test_uses_shared_gateway_only_for_llm_d_router_and_not_evaluation_owned():
    router = _execution(contract={"data_plane_kind": "llm-d-router"})
    assert deployment_uses_shared_gateway(router) is True
    owned = _execution(provenance={"evaluate_workflow": True}, contract={"data_plane_kind": "llm-d-router"})
    assert deployment_uses_shared_gateway(owned) is False
    assert deployment_uses_shared_gateway(_execution(contract={"data_plane_kind": "direct"})) is False
    assert deployment_uses_shared_gateway(_execution(contract={"data_plane_kind": "external"})) is False


def test_uses_shared_gateway_prefers_the_recorded_concrete_data_plane():
    # A llm-d-router guide that fell back to its own proxy because the
    # cluster had no ready shared Gateway at deploy time must stay on its
    # own proxy for benchmarks too, even though the guide's static kind is
    # still "llm-d-router".
    standalone = _execution(contract={"data_plane_kind": "llm-d-router"}, data_plane="standalone_router")
    assert deployment_uses_shared_gateway(standalone) is False
    shared = _execution(contract={"data_plane_kind": "llm-d-router"}, data_plane="shared_gateway")
    assert deployment_uses_shared_gateway(shared) is True
    # Evaluation-owned deployments stay on their own proxy regardless of what
    # was recorded.
    owned_shared = _execution(
        provenance={"evaluate_workflow": True},
        contract={"data_plane_kind": "llm-d-router"},
        data_plane="shared_gateway",
    )
    assert deployment_uses_shared_gateway(owned_shared) is False


def test_router_data_plane_args_always_force_plaintext_and_gate_disables_proxy():
    plaintext = ["--values", str(PLAINTEXT_EPP_VALUES_PATH)]
    shared = [*plaintext, "--values", str(GATEWAY_MODE_VALUES_PATH)]
    assert router_data_plane_args({}) == shared
    assert router_data_plane_args({"provenance": {"evaluate_workflow": True}}) == plaintext
    assert router_data_plane_args({"provenance": {"evaluation_id": "eval-1"}}) == plaintext
    assert router_data_plane_args(None) == shared


@pytest.mark.asyncio
async def test_resolve_data_plane_direct_and_external_have_no_gateway_lookup():
    assert await resolve_data_plane("direct", provenance={}, cluster_id="cluster-1") == ("direct", None)
    assert await resolve_data_plane("external", provenance={}, cluster_id="cluster-1") == ("external", None)


@pytest.mark.asyncio
async def test_resolve_data_plane_evaluation_owned_skips_gateway_lookup(monkeypatch):
    def boom(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("gateway lookup must be skipped for evaluation-owned deployments")

    monkeypatch.setattr(
        "llm_d_bench.model_service.gateway_ops.GatewayOpsService.cluster_gateway_base_url", boom
    )
    assert await resolve_data_plane(
        "llm-d-router", provenance={"evaluate_workflow": True}, cluster_id="cluster-1"
    ) == ("standalone_router", None)


@pytest.mark.asyncio
async def test_resolve_data_plane_uses_a_ready_shared_gateway(monkeypatch):
    async def ready(_self, _cluster_id):
        return "http://gateway:30012/v1"

    monkeypatch.setattr(
        "llm_d_bench.model_service.gateway_ops.GatewayOpsService.cluster_gateway_base_url", ready
    )
    assert await resolve_data_plane("llm-d-router", provenance={}, cluster_id="cluster-1") == (
        "shared_gateway",
        None,
    )


@pytest.mark.asyncio
async def test_resolve_data_plane_falls_back_when_gateway_not_ready(monkeypatch):
    async def not_ready(_self, _cluster_id):
        return None

    monkeypatch.setattr(
        "llm_d_bench.model_service.gateway_ops.GatewayOpsService.cluster_gateway_base_url", not_ready
    )
    data_plane, warning = await resolve_data_plane("llm-d-router", provenance={}, cluster_id="cluster-1")
    assert data_plane == "standalone_router"
    assert warning and "own router proxy" in warning


@pytest.mark.asyncio
async def test_resolve_data_plane_without_a_cluster_falls_back():
    data_plane, warning = await resolve_data_plane("llm-d-router", provenance={}, cluster_id=None)
    assert data_plane == "standalone_router"
    assert warning is not None


def test_router_data_plane_args_respect_the_resolved_data_plane():
    plaintext = ["--values", str(PLAINTEXT_EPP_VALUES_PATH)]
    shared = [*plaintext, "--values", str(GATEWAY_MODE_VALUES_PATH)]
    assert router_data_plane_args({"data_plane": "shared_gateway"}) == shared
    assert router_data_plane_args({"data_plane": "standalone_router"}) == plaintext
    assert router_data_plane_args({"data_plane": "direct"}) == []


def test_effective_values_force_plaintext_epp_for_standalone_too():
    import yaml

    content = yaml.safe_dump({"router": {"saved": True, "epp": {"flags": {"v": 2}}}})
    merged = yaml.safe_load(router_data_plane_effective_values(content, {"data_plane": "standalone_router"}))
    assert merged["router"]["epp"]["flags"]["secure-serving"] is False
    assert "proxy" not in merged["router"]

    shared = yaml.safe_load(router_data_plane_effective_values(content, {"data_plane": "shared_gateway"}))
    assert shared["router"]["epp"]["flags"]["secure-serving"] is False
    assert shared["router"]["proxy"]["enabled"] is False

    assert router_data_plane_effective_values(content, {"data_plane": "direct"}) == content
