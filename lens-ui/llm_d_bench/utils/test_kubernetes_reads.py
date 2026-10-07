"""Exercise the public read facade against a real SDK and a local API server."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from aiohttp import web

from llm_d_bench.cluster import registry, sessions
from llm_d_bench.utils import kubernetes
from llm_d_bench.utils.shell import CommandResult


@pytest_asyncio.fixture
async def cluster_api(tmp_path, monkeypatch):
    monkeypatch.delenv("PRISM_KUBERNETES_BACKEND", raising=False)
    calls = []
    response = {"items": [{"metadata": {"name": "worker", "resourceVersion": "42"}}]}
    state = SimpleNamespace(status=200, response=response, delay=0, calls=calls, methods=[])

    async def handle(request):
        state.methods.append(request.method)
        calls.append((request.path, dict(request.query), request.headers.get("Authorization")))
        if state.delay:
            await asyncio.sleep(state.delay)
        payload = state.response(request) if callable(state.response) else state.response
        return web.json_response(payload, status=state.status)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]

    def write_config(name="a", namespace="selected", credential="test-a"):
        path = tmp_path / f"{name}.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": "active",
                    "clusters": [{"name": "local", "cluster": {"server": f"http://{host}:{port}"}}],
                    "users": [{"name": "reader", "user": {"token": credential}}],
                    "contexts": [
                        {
                            "name": "active",
                            "context": {
                                "cluster": "local",
                                "user": "reader",
                                "namespace": namespace,
                            },
                        }
                    ],
                }
            )
        )
        return path

    state.write_config = write_config
    state.config = write_config()
    monkeypatch.setenv("KUBECONFIG", str(state.config))
    monkeypatch.setenv("PRISM_KUBERNETES_READ_BACKEND", "sdk")

    class ForbiddenRunner:
        async def run(self, *_args, **_kwargs):
            pytest.fail("SDK-supported reads must not execute kubectl")

    monkeypatch.setattr(kubernetes, "scoped_runner", lambda _cluster: ForbiddenRunner())
    monkeypatch.setattr(registry, "get_cluster", lambda key: SimpleNamespace(id=key))
    monkeypatch.setattr(registry, "kubeconfig_path", lambda key: tmp_path / f"{key}.yaml")
    monkeypatch.setattr(sessions, "session_kubeconfig_path", lambda key: tmp_path / f"session-{key}.yaml")
    monkeypatch.setattr(sessions, "get_session_for_server", lambda key: None)
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "path"),
    [
        ("nodes", "/api/v1/nodes"),
        ("pods", "/api/v1/namespaces/selected/pods"),
        ("services", "/api/v1/namespaces/selected/services"),
        ("deployments", "/apis/apps/v1/namespaces/selected/deployments"),
        ("persistentvolumeclaims", "/api/v1/namespaces/selected/persistentvolumeclaims"),
    ],
)
async def test_sdk_preserves_raw_fields_and_context_namespace(cluster_api, resource, path):
    result = await kubernetes.list_resources(resource, selector="app=demo", cluster_id="a")
    assert result == [{"metadata": {"name": "worker", "resourceVersion": "42"}}]
    assert cluster_api.calls == [(path, {"labelSelector": "app=demo", "limit": "500"}, "Bearer test-a")]


@pytest.mark.asyncio
async def test_sdk_pagination_and_all_namespaces_override(cluster_api):
    cluster_api.response = lambda req: (
        {"items": [{"metadata": {"name": "second"}}]}
        if req.query.get("continue")
        else {"items": [{"metadata": {"name": "first"}}], "metadata": {"continue": "next/token"}}
    )
    result = await kubernetes.list_resources("pods", namespace="ignored", all_namespaces=True)
    assert [item["metadata"]["name"] for item in result] == ["first", "second"]
    assert [call[0] for call in cluster_api.calls] == ["/api/v1/pods", "/api/v1/pods"]
    assert cluster_api.calls[1][1]["continue"] == "next/token"


@pytest.mark.asyncio
async def test_explicit_namespace_and_independent_credentials(cluster_api):
    cluster_api.write_config("b", "other", "test-b")
    await asyncio.gather(
        kubernetes.list_resources("pods", cluster_id="a", namespace="override"),
        kubernetes.list_resources("pods", cluster_id="b"),
    )
    assert {(path, token) for path, _, token in cluster_api.calls} == {
        ("/api/v1/namespaces/override/pods", "Bearer test-a"),
        ("/api/v1/namespaces/other/pods", "Bearer test-b"),
    }
    cluster_api.write_config("a", "updated", "rotated")
    await kubernetes.list_resources("pods", cluster_id="a")
    assert cluster_api.calls[-1][0] == "/api/v1/namespaces/updated/pods"
    assert cluster_api.calls[-1][2] == "Bearer rotated"


@pytest.mark.asyncio
async def test_missing_selected_config_cannot_use_ambient_cluster(cluster_api):
    with pytest.raises(FileNotFoundError):
        await kubernetes.list_resources("nodes", cluster_id="missing")
    assert cluster_api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_api_errors_preserve_empty_list_without_cli_retry_or_secret_logs(cluster_api, caplog, status):
    cluster_api.status = status
    cluster_api.response = {"message": "sensitive-server-response"}
    assert await kubernetes.list_resources("nodes") == []
    assert len(cluster_api.calls) == 1
    assert str(status) in caplog.text
    assert "sensitive-server-response" not in caplog.text
    assert "test-a" not in caplog.text


@pytest.mark.asyncio
async def test_sdk_default_and_cli_rollback_preserve_arguments(cluster_api, monkeypatch):
    commands = []

    class Runner:
        async def run(self, argv):
            commands.append(argv)
            return CommandResult(
                argv=tuple(argv), returncode=0, stdout=json.dumps({"items": [{"cli": True}]}), stderr=""
            )

    monkeypatch.setattr(kubernetes, "scoped_runner", lambda _: Runner())
    monkeypatch.delenv("PRISM_KUBERNETES_READ_BACKEND")
    assert await kubernetes.list_resources("pods") == cluster_api.response["items"]
    monkeypatch.setenv("PRISM_KUBERNETES_BACKEND", "cli")
    assert await kubernetes.list_resources("pods", namespace="chosen", selector="x=y") == [{"cli": True}]
    monkeypatch.delenv("PRISM_KUBERNETES_BACKEND")
    monkeypatch.setenv("PRISM_KUBERNETES_READ_BACKEND", "sdk")
    assert await kubernetes.list_resources("pods") == cluster_api.response["items"]
    monkeypatch.setenv("PRISM_KUBERNETES_READ_BACKEND", "cli")
    assert await kubernetes.list_resources("pods", namespace="chosen", selector="x=y") == [{"cli": True}]
    assert commands == [["kubectl", "get", "pods", "-n", "chosen", "-l", "x=y", "-o", "json"]] * 2


@pytest.mark.asyncio
async def test_unmigrated_resource_routes_to_cli_before_execution(cluster_api, monkeypatch):
    class Runner:
        async def run(self, argv):
            assert argv == ["kubectl", "get", "pods/status", "-o", "json"]
            return CommandResult(argv=tuple(argv), returncode=0, stdout='{"items":[{"legacy":true}]}', stderr="")

    monkeypatch.setattr(kubernetes, "scoped_runner", lambda _: Runner())
    assert await kubernetes.list_resources("pods/status") == [{"legacy": True}]
    assert cluster_api.calls == []


@pytest.mark.asyncio
async def test_invalid_backend_is_configuration_error(cluster_api, monkeypatch):
    monkeypatch.setenv("PRISM_KUBERNETES_READ_BACKEND", "sdkk")
    with pytest.raises(ValueError, match="PRISM_KUBERNETES"):
        await kubernetes.list_resources("nodes")
    assert cluster_api.calls == []


@pytest.mark.asyncio
async def test_default_namespace_when_context_does_not_select_one(cluster_api):
    cluster_api.write_config(namespace="")
    await kubernetes.list_resources("po")
    assert cluster_api.calls[0][0] == "/api/v1/namespaces/default/pods"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": "invalid"},
        {"items": [None]},
        {"items": [], "metadata": {"continue": "repeated"}},
    ],
)
async def test_malformed_or_repeated_pages_do_not_return_partial_success(cluster_api, caplog, payload):
    cluster_api.response = payload
    assert await kubernetes.list_resources("nodes") == []
    assert "category=response" in caplog.text
    assert len(cluster_api.calls) <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_close_sdk_connections(cluster_api, monkeypatch, caplog, cancel):
    from llm_d_bench.utils import kubernetes_pool, kubernetes_reads

    clients = []
    original = kubernetes_pool.client.ApiClient

    def capture_client(*args, **kwargs):
        api = original(*args, **kwargs)
        clients.append(api)
        return api

    monkeypatch.setattr(kubernetes_pool.client, "ApiClient", capture_client)
    monkeypatch.setattr(kubernetes_reads, "_READ_TIMEOUT", 0.1 if not cancel else 5)
    cluster_api.delay = 0.3
    task = asyncio.create_task(kubernetes.list_resources("nodes"))
    if cancel:
        async with asyncio.timeout(2):
            while not cluster_api.calls:
                await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == []
        assert "category=timeout" in caplog.text
    assert clients and all(api.rest_client.pool_manager.closed for api in clients)


@pytest.mark.asyncio
async def test_storage_node_discovery_uses_sdk_without_changing_domain_result(cluster_api):
    from llm_d_bench.storage.service import discover_nodes

    cluster_api.response = {
        "items": [
            {"metadata": {"name": "worker"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {
                "metadata": {"name": "control"},
                "spec": {
                    "taints": [
                        {"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"},
                    ]
                },
            },
        ]
    }
    nodes = await discover_nodes("a")
    assert [(node.name, node.ready, node.schedulable) for node in nodes] == [
        ("worker", True, True),
        ("control", False, False),
    ]


@pytest.mark.asyncio
async def test_session_config_and_deleted_session_do_not_leak_credentials(cluster_api, monkeypatch):
    session_config = cluster_api.write_config("session-s", "session-ns", "session-token")
    monkeypatch.setattr(registry, "get_cluster", lambda key: None)
    await kubernetes.list_resources("pods", cluster_id="s")
    assert cluster_api.calls[-1][2] == "Bearer session-token"
    session_config.unlink()
    with pytest.raises(FileNotFoundError):
        await kubernetes.list_resources("pods", cluster_id="s")
    assert len(cluster_api.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authentication",
    [
        {"auth-provider": {"name": "gcp"}},
    ],
)
async def test_dynamic_credentials_route_to_cli_before_sdk_authentication(
    cluster_api,
    monkeypatch,
    caplog,
    authentication,
):
    document = yaml.safe_load(cluster_api.config.read_text())
    document["users"][0]["user"] = authentication
    cluster_api.config.write_text(yaml.safe_dump(document))

    class Runner:
        async def run(self, argv):
            assert argv == ["kubectl", "get", "nodes", "-o", "json"]
            return CommandResult(argv=tuple(argv), returncode=0, stdout='{"items":[{"via":"cli"}]}', stderr="")

    monkeypatch.setattr(kubernetes, "scoped_runner", lambda _: Runner())
    assert await kubernetes.list_resources("nodes") == [{"via": "cli"}]
    assert cluster_api.calls == []
    assert "/must-not-execute" not in caplog.text


@pytest.mark.asyncio
async def test_merged_config_uses_selected_static_identity_only(cluster_api, monkeypatch, tmp_path):
    document = yaml.safe_load(cluster_api.config.read_text())
    credentials = {"apiVersion": "v1", "kind": "Config", "users": document.pop("users")}
    document["users"] = [
        {
            "name": "unselected",
            "user": {
                "exec": {"command": "/unselected-helper-must-not-run", "apiVersion": "client.authentication.k8s.io/v1"},
            },
        }
    ]
    cluster_api.config.write_text(yaml.safe_dump(document))
    credential_path = tmp_path / "credentials.yaml"
    credential_path.write_text(yaml.safe_dump(credentials))
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join((str(cluster_api.config), str(credential_path))))
    assert await kubernetes.list_resources("nodes") == [{"metadata": {"name": "worker", "resourceVersion": "42"}}]
    assert cluster_api.calls[-1][2] == "Bearer test-a"
