"""Mutation semantics at the official SDK call boundary; no live cluster writes."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from kubernetes.aio.client.exceptions import ApiException

from llm_d_bench.utils import kubernetes_mutations as mutations


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(calls=[], responses=[], closed=False, kubeconfigs=[])

    async def call_api(path, method, **kwargs):
        state.calls.append((path, method, kwargs))
        response = state.responses.pop(0) if state.responses else {}
        if isinstance(response, Exception):
            raise response
        return response

    @asynccontextmanager
    async def session(kubeconfig):
        state.kubeconfigs.append(kubeconfig)
        try:
            yield SimpleNamespace(call_api=call_api), {"context": {"namespace": "selected"}}
        finally:
            state.closed = True

    async def resolve(_api, resource):
        namespaced = resource not in {"namespaces", "nodes", "pv"}

        def path(namespace=None, name=None):
            base = "/api/v1"
            if namespaced and namespace:
                base += f"/namespaces/{namespace}"
            base += f"/{resource}"
            return base + (f"/{name}" if name else "")

        return SimpleNamespace(path=path, namespaced=namespaced)

    monkeypatch.setattr(mutations, "api_session", session)
    monkeypatch.setattr(mutations, "resolve_resource", resolve)
    return state


@pytest.mark.asyncio
async def test_create_uses_explicit_cluster_and_context_namespace(sdk):
    body = {"metadata": {"name": "example"}}
    sdk.responses = [body]
    assert await mutations.create_sdk_resource("jobs", body, kubeconfig="/cluster-a") == body
    path, method, args = sdk.calls[0]
    assert (path, method) == ("/api/v1/namespaces/selected/jobs", "POST")
    assert args["body"] == body
    assert sdk.kubeconfigs == ["/cluster-a"]
    assert sdk.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("patch_type", ["merge", "strategic", "json"])
async def test_patch_preserves_content_type(sdk, patch_type):
    patch = [{"op": "remove", "path": "/metadata/labels/a"}] if patch_type == "json" else {"spec": {"replicas": 2}}
    await mutations.patch_sdk_resource("deployments", "app", patch, patch_type=patch_type, namespace="other")
    path, method, args = sdk.calls[0]
    assert path == "/api/v1/namespaces/other/deployments/app"
    assert method == "PATCH"
    assert args["body"] == patch
    assert args["header_params"]["Content-Type"] == mutations._PATCH_TYPES[patch_type]


@pytest.mark.asyncio
async def test_label_no_overwrite_checks_version(sdk):
    sdk.responses = [{"metadata": {"resourceVersion": "7", "labels": {"old": "value"}}}, {}]
    await mutations.label_sdk_resource("nodes", "worker", {"new": "value", "old": None}, overwrite=False)
    assert sdk.calls[1][2]["body"] == {
        "metadata": {"resourceVersion": "7", "labels": {"new": "value", "old": None}},
    }


@pytest.mark.asyncio
async def test_label_collision_does_not_write(sdk):
    sdk.responses = [{"metadata": {"labels": {"key": "old"}}}]
    with pytest.raises(ApiException) as caught:
        await mutations.label_sdk_resource("nodes", "worker", {"key": "new"}, overwrite=False)
    assert caught.value.status == 409
    assert len(sdk.calls) == 1


@pytest.mark.asyncio
async def test_delete_waits_for_original_uid_only(sdk):
    sdk.responses = [{"details": {"uid": "old"}}, {"metadata": {"uid": "new"}}]
    await mutations.delete_sdk_resource("jobs", "job")
    assert [call[1] for call in sdk.calls] == ["DELETE", "GET"]
    assert sdk.calls[0][2]["body"]["propagationPolicy"] == "Background"


@pytest.mark.asyncio
async def test_delete_waits_for_not_found(sdk):
    sdk.responses = [{"metadata": {"uid": "old"}}, ApiException(status=404)]
    await mutations.delete_sdk_resource("namespaces", "old")
    assert sdk.calls[0][0] == "/api/v1/namespaces/old"
    assert len(sdk.calls) == 2


@pytest.mark.asyncio
async def test_delete_no_wait_does_not_poll(sdk):
    await mutations.delete_sdk_resource("jobs", "job", wait=False)
    assert len(sdk.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,ignore,raises", [(404, True, False), (404, False, True), (403, True, True), (409, True, True)]
)
async def test_delete_ignores_only_requested_not_found(sdk, status, ignore, raises):
    sdk.responses = [ApiException(status=status)]
    if raises:
        with pytest.raises(ApiException):
            await mutations.delete_sdk_resource("jobs", "job", ignore_not_found=ignore)
    else:
        assert await mutations.delete_sdk_resource("jobs", "job", ignore_not_found=ignore) == {}
    assert len(sdk.calls) == 1
    assert sdk.closed


@pytest.mark.asyncio
async def test_bulk_deletion_paginates_and_preserves_namespaces(sdk):
    sdk.responses = [
        {"items": [{"metadata": {"name": "one", "namespace": "a"}}], "metadata": {"continue": "next"}},
        {"items": [{"metadata": {"name": "two", "namespace": "b"}}]},
        {},
        {},
    ]
    await mutations.delete_sdk_resources("pvc", all_namespaces=True, selector="volume=id", wait=False)
    assert sdk.calls[0][0] == "/api/v1/pvc"
    assert sdk.calls[1][2]["query_params"] == [("limit", 500), ("labelSelector", "volume=id"), ("continue", "next")]
    assert [call[0] for call in sdk.calls[2:]] == ["/api/v1/namespaces/a/pvc/one", "/api/v1/namespaces/b/pvc/two"]


@pytest.mark.asyncio
async def test_timeout_prevents_unbounded_request(sdk):
    with pytest.raises(TimeoutError):
        await mutations.delete_sdk_resource("jobs", "job", timeout=0)
    assert not sdk.calls


@pytest.mark.asyncio
async def test_scale_uses_scale_subresource(sdk):
    await mutations.scale_sdk_resource("deployments", "app", 3)
    assert sdk.calls[0][0].endswith("/deployments/app/scale")
    assert sdk.calls[0][2]["body"] == {"spec": {"replicas": 3}}


@pytest.mark.asyncio
async def test_real_sdk_mutations_against_local_http(tmp_path):
    """Exercise SDK serialization/authentication without contacting a real cluster."""
    import json

    from aiohttp import web

    calls = []

    async def handle(request):
        body = await request.json() if request.can_read_body else None
        calls.append((request.method, request.path, request.headers, body))
        if request.method == "GET":
            return web.json_response({"kind": "Status", "reason": "NotFound"}, status=404)
        return web.json_response({"metadata": {"uid": "created"}}, status=201 if request.method == "POST" else 200)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "local",
                "clusters": [{"name": "local", "cluster": {"server": f"http://{host}:{port}"}}],
                "users": [{"name": "writer", "user": {"token": "fixture-token"}}],
                "contexts": [
                    {
                        "name": "local",
                        "context": {
                            "cluster": "local",
                            "user": "writer",
                            "namespace": "selected",
                        },
                    }
                ],
            }
        )
    )
    try:
        await mutations.create_sdk_resource("namespace", {"metadata": {"name": "test"}}, kubeconfig=str(config))
        await mutations.patch_sdk_resource(
            "jobs", "job", {"metadata": {"labels": {"key": "value"}}}, patch_type="merge", kubeconfig=str(config)
        )
        await mutations.delete_sdk_resource("jobs", "job", kubeconfig=str(config))
    finally:
        await runner.cleanup()
    assert [(method, path) for method, path, _, _ in calls] == [
        ("POST", "/api/v1/namespaces"),
        ("PATCH", "/apis/batch/v1/namespaces/selected/jobs/job"),
        ("DELETE", "/apis/batch/v1/namespaces/selected/jobs/job"),
        ("GET", "/apis/batch/v1/namespaces/selected/jobs/job"),
    ]
    assert all(headers["Authorization"] == "Bearer fixture-token" for _, _, headers, _ in calls)
    assert calls[1][2]["Content-Type"] == "application/merge-patch+json"
    assert calls[1][3] == {"metadata": {"labels": {"key": "value"}}}
    assert calls[2][3]["propagationPolicy"] == "Background"
