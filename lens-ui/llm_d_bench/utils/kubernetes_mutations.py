"""Explicit Kubernetes writes using the shared official-SDK session.

Client-side apply remains a kubectl operation. These helpers never retry writes
or fall back to a subprocess after submitting a request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from kubernetes.aio.client.exceptions import ApiException

from llm_d_bench.utils.kubernetes_api import api_session, list_from_api, resolve_resource, selected_namespace

_PATCH_TYPES = {
    "merge": "application/merge-patch+json",
    "strategic": "application/strategic-merge-patch+json",
    "json": "application/json-patch+json",
}


async def _request(
    api, path: str, method: str, *, body=None, content_type="application/json", query=None, timeout: float | None = 30
) -> dict:
    if timeout is not None and timeout <= 0:
        raise TimeoutError("Kubernetes mutation deadline exceeded")
    return await api.call_api(
        path,
        method,
        body=body,
        query_params=query or [],
        header_params={"Accept": "application/json", "Content-Type": content_type},
        response_types_map={200: "object", 201: "object", 202: "object"},
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
        _request_timeout=timeout,
    )


async def create_sdk_resource(
    resource: str,
    body: dict,
    *,
    kubeconfig: str | None = None,
    namespace: str | None = None,
    timeout: float | None = 30,
) -> dict:
    """POST one object; AlreadyExists and other API failures propagate."""
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        resolved = await resolve_resource(api, resource)
        path = resolved.path(namespace=selected_namespace(context, namespace) if resolved.namespaced else None)
        return await _request(api, path, "POST", body=body, timeout=timeout)


async def patch_sdk_resource(
    resource: str,
    name: str,
    patch: dict | list,
    *,
    patch_type: str = "strategic",
    kubeconfig: str | None = None,
    namespace: str | None = None,
    timeout: float | None = 30,
) -> dict:
    """PATCH without converting between JSON, merge and strategic patch semantics."""
    if patch_type not in _PATCH_TYPES:
        raise ValueError("patch_type must be merge, strategic or json")
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        resolved = await resolve_resource(api, resource)
        path = resolved.path(
            namespace=selected_namespace(context, namespace) if resolved.namespaced else None, name=name
        )
        return await _request(api, path, "PATCH", body=patch, content_type=_PATCH_TYPES[patch_type], timeout=timeout)


async def label_sdk_resource(
    resource: str,
    name: str,
    labels: Mapping[str, str | None],
    *,
    overwrite: bool = True,
    kubeconfig: str | None = None,
    namespace: str | None = None,
    timeout: float | None = 30,
) -> dict:
    """Merge labels with an optimistic concurrency guard when overwrite is refused."""
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        resolved = await resolve_resource(api, resource)
        path = resolved.path(
            namespace=selected_namespace(context, namespace) if resolved.namespaced else None, name=name
        )
        metadata: dict[str, Any] = {"labels": dict(labels)}
        if not overwrite:
            existing = await _request(api, path, "GET", timeout=timeout)
            old_metadata = existing.get("metadata") or {}
            old_labels = old_metadata.get("labels") or {}
            for key, value in labels.items():
                if value is not None and key in old_labels and old_labels[key] != value:
                    raise ApiException(status=409, reason=f"label {key!r} already has a value; overwrite is disabled")
            if old_metadata.get("resourceVersion"):
                metadata["resourceVersion"] = old_metadata["resourceVersion"]
        return await _request(
            api, path, "PATCH", body={"metadata": metadata}, content_type=_PATCH_TYPES["merge"], timeout=timeout
        )


async def scale_sdk_resource(
    resource: str,
    name: str,
    replicas: int,
    *,
    kubeconfig: str | None = None,
    namespace: str | None = None,
    timeout: float | None = 30,
) -> dict:
    """Update the scale subresource, keeping workload kind-specific spec handling server-side."""
    if replicas < 0:
        raise ValueError("replicas must be non-negative")
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        resolved = await resolve_resource(api, resource)
        path = resolved.path(
            namespace=selected_namespace(context, namespace) if resolved.namespaced else None, name=name
        )
        return await _request(
            api,
            path + "/scale",
            "PATCH",
            body={"spec": {"replicas": replicas}},
            content_type=_PATCH_TYPES["merge"],
            timeout=timeout,
        )


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - asyncio.get_running_loop().time()


async def _delete(
    api, path: str, *, ignore_not_found: bool, wait: bool, deadline: float | None, propagation_policy: str
) -> dict:
    try:
        deleted = await _request(
            api,
            path,
            "DELETE",
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": propagation_policy,
            },
            timeout=_remaining(deadline),
        )
    except ApiException as error:
        if error.status == 404 and ignore_not_found:
            return {}
        raise
    if not wait:
        return deleted
    uid = (deleted.get("metadata") or {}).get("uid") or (deleted.get("details") or {}).get("uid")
    while True:
        try:
            current = await _request(api, path, "GET", timeout=_remaining(deadline))
        except ApiException as error:
            if error.status == 404:
                return deleted
            raise
        if uid and (current.get("metadata") or {}).get("uid") != uid:
            return deleted
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            raise TimeoutError("Timed out waiting for Kubernetes object deletion")
        await asyncio.sleep(min(0.2, remaining) if remaining is not None else 0.2)


async def delete_sdk_resource(
    resource: str,
    name: str,
    *,
    kubeconfig: str | None = None,
    namespace: str | None = None,
    ignore_not_found: bool = False,
    wait: bool = True,
    timeout: float | None = 60,
    propagation_policy: str = "Background",
) -> dict:
    """Delete an object and optionally wait until its UID disappears, including finalizers."""
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        resolved = await resolve_resource(api, resource)
        path = resolved.path(
            namespace=selected_namespace(context, namespace) if resolved.namespaced else None, name=name
        )
        return await _delete(
            api,
            path,
            ignore_not_found=ignore_not_found,
            wait=wait,
            deadline=deadline,
            propagation_policy=propagation_policy,
        )


async def delete_sdk_resources(
    resource: str,
    *,
    kubeconfig: str | None = None,
    namespace: str | None = None,
    selector: str | None = None,
    all_namespaces: bool = False,
    ignore_not_found: bool = False,
    wait: bool = True,
    timeout: float | None = 60,
    propagation_policy: str = "Background",
) -> list[dict]:
    """List matching objects then delete each within one deadline and cluster session."""
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        resolved = await resolve_resource(api, resource)
        ns = selected_namespace(context, namespace) if resolved.namespaced and not all_namespaces else None
        page = await list_from_api(api, resolved, namespace=ns, selector=selector)
        items = page["items"]
        deleted = []
        for item in items:
            metadata = item["metadata"]
            path = resolved.path(
                namespace=metadata.get("namespace", ns) if resolved.namespaced else None, name=metadata["name"]
            )
            deleted.append(
                await _delete(
                    api,
                    path,
                    ignore_not_found=ignore_not_found,
                    wait=wait,
                    deadline=deadline,
                    propagation_policy=propagation_policy,
                )
            )
        return deleted
