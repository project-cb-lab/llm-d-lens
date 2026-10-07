"""Real SDK watch streams against a local aiohttp server, without a cluster."""

import asyncio
import json
import sys
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from aiohttp import web
from kubernetes.aio.client.exceptions import ApiException

from llm_d_bench.utils import kubernetes_watch as watch


def event(kind, version, name="pod"):
    return {"type": kind, "object": {"metadata": {"name": name, "resourceVersion": version}}}


@pytest_asyncio.fixture
async def server(tmp_path, monkeypatch):
    state = SimpleNamespace(
        calls=[], authorization=[], lists=0, watches=0, batches=[], hold=False, connected=asyncio.Event()
    )

    async def handle(request):
        state.calls.append((request.path, dict(request.query)))
        state.authorization.append(request.headers.get("Authorization"))
        if request.query.get("watch") != "true":
            state.lists += 1
            return web.json_response(
                {
                    "metadata": {"resourceVersion": str(state.lists * 10)},
                    "items": [
                        {"metadata": {"name": f"initial-{state.lists}", "resourceVersion": str(state.lists * 10)}}
                    ],
                }
            )
        index = state.watches
        state.watches += 1
        batch = state.batches[index] if index < len(state.batches) else []
        if isinstance(batch, int):
            return web.Response(status=batch)
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        state.connected.set()
        for item in batch:
            await response.write(json.dumps(item).encode() + b"\n")
        if state.hold:
            while request.transport is not None and not request.transport.is_closing():
                await asyncio.sleep(0.01)
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    config = tmp_path / "config"
    config.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "test",
                "contexts": [{"name": "test", "context": {"cluster": "test", "namespace": "selected"}}],
                "clusters": [{"name": "test", "cluster": {"server": f"http://{host}:{port}"}}],
            }
        )
    )
    state.config = str(config)
    monkeypatch.setattr(watch, "_BACKOFF_INITIAL", 0.01)
    monkeypatch.setattr(watch, "_BACKOFF_MAX", 0.04)
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_initial_snapshot_bookmark_resume_and_selector(server):
    server.batches = [[event("MODIFIED", "11"), event("BOOKMARK", "12")], [event("DELETED", "13")]]
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config, selector="app=x")) as events:
        assert (await anext(events))["object"]["metadata"]["name"] == "initial-1"
        assert (await anext(events))["type"] == "MODIFIED"
        assert (await anext(events))["type"] == "DELETED"
    assert server.calls[0] == ("/api/v1/namespaces/selected/pods", {"limit": "500", "labelSelector": "app=x"})
    assert server.calls[1][1]["resourceVersion"] == "10"
    assert server.calls[2][1]["resourceVersion"] == "12"
    assert server.calls[1][1]["timeoutSeconds"] == "30"


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [410, [{"type": "ERROR", "object": {"code": 410}}]])
async def test_expired_version_resets_and_relists(server, expired):
    server.batches = [expired, [event("MODIFIED", "21")]]
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config, all_namespaces=True)) as events:
        assert (await anext(events))["type"] == "ADDED"
        assert await anext(events) == {"type": "RESET", "object": None}
        assert (await anext(events))["object"]["metadata"]["name"] == "initial-2"
        assert (await anext(events))["type"] == "MODIFIED"
    assert server.lists == 2
    assert all(path == "/api/v1/pods" for path, _ in server.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_surface_without_reconnect(server, status):
    server.batches = [status]
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config)) as events:
        await anext(events)
        with pytest.raises(ApiException) as error:
            await anext(events)
    assert error.value.status == status
    assert server.watches == 1


@pytest.mark.asyncio
async def test_transient_status_reconnects(server):
    server.batches = [503, [event("ADDED", "11")]]
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config)) as events:
        await anext(events)
        assert (await anext(events))["object"]["metadata"]["resourceVersion"] == "11"
    assert server.watches == 2
    assert server.lists == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stop_and_cancellation_close_stream(server, monkeypatch, cancel):
    server.hold = True
    stop = asyncio.Event()
    responses = []
    from aiohttp import ClientResponse

    original_close = ClientResponse.close

    def close(response):
        responses.append(response)
        return original_close(response)

    monkeypatch.setattr(ClientResponse, "close", close)
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config, stop=stop)) as events:
        await anext(events)
        pending = asyncio.create_task(anext(events))
        await asyncio.wait_for(server.connected.wait(), 2)
        if cancel:
            pending.cancel()
        else:
            stop.set()
        with pytest.raises(asyncio.CancelledError if cancel else StopAsyncIteration):
            await asyncio.wait_for(pending, 2)
    assert any(response.url.query.get("watch") == "true" and response.closed for response in responses)


@pytest.mark.asyncio
async def test_pre_stopped_watch_makes_no_requests(server):
    stop = asyncio.Event()
    stop.set()
    assert [item async for item in watch.watch_resource_events("pods", kubeconfig=server.config, stop=stop)] == []
    assert server.calls == []


@pytest.mark.asyncio
async def test_clean_empty_streams_have_backoff(server, monkeypatch):
    monkeypatch.setattr(watch, "_BACKOFF_INITIAL", 0.2)
    stop = asyncio.Event()
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config, stop=stop)) as events:
        await anext(events)
        pending = asyncio.create_task(anext(events))
        await asyncio.wait_for(server.connected.wait(), 2)
        await asyncio.sleep(0.04)
        stop.set()
        with pytest.raises(StopAsyncIteration):
            await pending
    assert server.watches == 1


@pytest.mark.asyncio
async def test_reconnect_reloads_exec_credentials(server, tmp_path):
    counter = tmp_path / "counter"
    helper = tmp_path / "credentials.py"
    helper.write_text(
        "import json\nfrom pathlib import Path\n"
        f"counter = Path({str(counter)!r})\n"
        "number = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(number))\n"
        'print(json.dumps({"kind": "ExecCredential", "apiVersion": "client.authentication.k8s.io/v1", '
        '"status": {"token": "token-" + str(number)}}))\n'
    )
    config = Path(server.config)
    document = yaml.safe_load(config.read_text())
    document["contexts"][0]["context"]["user"] = "exec-user"
    document["users"] = [
        {
            "name": "exec-user",
            "user": {
                "exec": {
                    "command": sys.executable,
                    "args": [str(helper)],
                    "interactiveMode": "Never",
                    "apiVersion": "client.authentication.k8s.io/v1",
                }
            },
        }
    ]
    config.write_text(yaml.safe_dump(document))
    server.batches = [[event("MODIFIED", "11")], [event("MODIFIED", "12")]]
    async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config)) as events:
        await anext(events)
        await anext(events)
        await anext(events)
    assert server.authorization == ["Bearer token-1", "Bearer token-1", "Bearer token-2"]
    assert counter.read_text() == "2"


@pytest.mark.asyncio
async def test_open_watch_does_not_occupy_request_pool(server, monkeypatch):
    from llm_d_bench.utils import kubernetes_api, kubernetes_pool

    monkeypatch.setattr(kubernetes_pool, "_MAX_CONCURRENCY", 1)
    await kubernetes_pool.start_pool()
    server.hold = True
    try:
        async with aclosing(watch.watch_resource_events("pods", kubeconfig=server.config)) as events:
            await anext(events)
            pending = asyncio.create_task(anext(events))
            try:
                await asyncio.wait_for(server.connected.wait(), 1)
                result = await asyncio.wait_for(
                    kubernetes_api.query_resource("pods", kubeconfig=server.config),
                    1,
                )
                assert result["items"]
            finally:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
    finally:
        await kubernetes_pool.close_pool()
