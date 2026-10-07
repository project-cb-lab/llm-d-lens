"""Planning reads cross the session boundary without accepting local paths."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from llm_d_bench.cluster import sdk_discovery
from llm_d_bench.utils.shell import CommandResult


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(sdk_discovery.router, prefix="/api/cluster")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,args",
    [
        ("version", ["version", "-o", "json"]),
        ("nodes", ["get", "nodes", "-o", "json"]),
        ("deviceclasses", ["get", "deviceclasses.resource.k8s.io", "-o", "json"]),
        ("resourceslices", ["get", "resourceslices.resource.k8s.io", "-o", "json"]),
    ],
)
async def test_only_allowlisted_session_reads(app, monkeypatch, resource, args):
    monkeypatch.setattr(sdk_discovery.sessions, "require_active_session", lambda value: SimpleNamespace(id=value))
    read = AsyncMock(return_value=CommandResult((), 0, json.dumps({"items": []}), ""))
    monkeypatch.setattr(sdk_discovery, "run_kubectl", read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/cluster/sessions/session-id/planning-discovery/{resource}")
    assert response.status_code == 200
    assert response.json() == {"result": {"items": []}}
    read.assert_awaited_once_with(args, timeout=10, cluster_id="session-id")


@pytest.mark.asyncio
async def test_inactive_session_cannot_read(app, monkeypatch):
    def inactive(value):
        raise ValueError("inactive")

    monkeypatch.setattr(sdk_discovery.sessions, "require_active_session", inactive)
    read = AsyncMock()
    monkeypatch.setattr(sdk_discovery, "run_kubectl", read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/cluster/sessions/not-a-session/planning-discovery/nodes")
    assert response.status_code == 404
    read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        CommandResult((), 1, "", "private error"),
        CommandResult((), 0, "not json", ""),
        CommandResult((), 0, "[]", ""),
        TimeoutError("private error"),
    ],
)
async def test_read_failures_remain_null(app, monkeypatch, result):
    monkeypatch.setattr(sdk_discovery.sessions, "require_active_session", lambda value: SimpleNamespace(id=value))
    read = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(sdk_discovery, "run_kubectl", read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/cluster/sessions/session-id/planning-discovery/nodes")
    assert response.json() == {"result": None}


@pytest.mark.asyncio
async def test_arbitrary_resources_are_rejected(app, monkeypatch):
    read = AsyncMock()
    monkeypatch.setattr(sdk_discovery, "run_kubectl", read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/cluster/sessions/session-id/planning-discovery/secrets")
    assert response.status_code == 422
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch):
    import asyncio

    monkeypatch.setattr(sdk_discovery.sessions, "require_active_session", lambda value: SimpleNamespace(id=value))
    monkeypatch.setattr(sdk_discovery, "run_kubectl", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await sdk_discovery.planning_discovery("session-id", "nodes")


@pytest.mark.asyncio
async def test_real_session_validation_rejects_missing_or_unsafe_config(app, monkeypatch, tmp_path):
    session_id = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(sdk_discovery.sessions, "_SESSION_DIRECTORY", tmp_path)
    read = AsyncMock()
    monkeypatch.setattr(sdk_discovery, "run_kubectl", read)
    route = f"/api/cluster/sessions/{session_id}/planning-discovery/nodes"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(route)).status_code == 404
        kubeconfig = tmp_path / f"{session_id}.yaml"
        kubeconfig.write_text("private")
        kubeconfig.chmod(0o644)
        assert (await client.get(route)).status_code == 404
    read.assert_not_awaited()
