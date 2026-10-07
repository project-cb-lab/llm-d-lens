"""Tests for ``_resolve_task_endpoint_url``'s shared-Gateway override.

A Model Service ("existing endpoint") task always targets its HTTPRoute
through the shared Gateway, regardless of whatever data plane the underlying
deployment rendered at deploy time. A Design Configuration task (no
``model_service_group_id``) instead relies solely on the deployment's own
recorded/declared data plane (``context.uses_shared_gateway``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_d_bench.simulation import service as simulation_service


def _execution_context(*, uses_shared_gateway: bool) -> SimpleNamespace:
    return SimpleNamespace(
        execution_id="exec-1",
        endpoint="http://deployment.local",
        namespace="ns",
        cluster_id="cluster-1",
        display_name="my-deployment",
        guide=None,
        uses_shared_gateway=uses_shared_gateway,
        configuration_artifact_ids=(),
    )


@pytest.mark.asyncio
async def test_force_gateway_overrides_the_endpoint_even_when_data_plane_says_standalone(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.deploy.executions.get_execution_context",
        lambda _execution_id: _execution_context(uses_shared_gateway=False),
    )
    monkeypatch.setattr("llm_d_bench.cluster.registry.get_cluster", lambda _cluster_id: None)
    monkeypatch.setattr(
        simulation_service, "_cluster_gateway_endpoint", AsyncMock(return_value="http://gateway.local")
    )

    resolved = await simulation_service._resolve_task_endpoint_url(
        endpoint_mode="deployment",
        endpoint_url="http://ignored.local",
        endpoint_deployment_execution_id="exec-1",
        force_gateway=True,
    )

    assert resolved.url == "http://gateway.local"


@pytest.mark.asyncio
async def test_without_force_gateway_the_deployments_own_data_plane_is_used(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.deploy.executions.get_execution_context",
        lambda _execution_id: _execution_context(uses_shared_gateway=False),
    )
    monkeypatch.setattr("llm_d_bench.cluster.registry.get_cluster", lambda _cluster_id: None)
    monkeypatch.setattr(
        simulation_service, "_cluster_gateway_endpoint", AsyncMock(return_value="http://gateway.local")
    )

    resolved = await simulation_service._resolve_task_endpoint_url(
        endpoint_mode="deployment",
        endpoint_url="http://ignored.local",
        endpoint_deployment_execution_id="exec-1",
        force_gateway=False,
    )

    assert resolved.url == "http://deployment.local"


@pytest.mark.asyncio
async def test_design_configuration_deployment_still_uses_its_own_shared_gateway_flag(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.deploy.executions.get_execution_context",
        lambda _execution_id: _execution_context(uses_shared_gateway=True),
    )
    monkeypatch.setattr("llm_d_bench.cluster.registry.get_cluster", lambda _cluster_id: None)
    monkeypatch.setattr(
        simulation_service, "_cluster_gateway_endpoint", AsyncMock(return_value="http://gateway.local")
    )

    resolved = await simulation_service._resolve_task_endpoint_url(
        endpoint_mode="deployment",
        endpoint_url="http://ignored.local",
        endpoint_deployment_execution_id="exec-1",
        force_gateway=False,
    )

    assert resolved.url == "http://gateway.local"
