"""Raw SDK list/watch events with explicit reset and bounded reconnects."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from aiohttp import ClientError, ClientTimeout
from kubernetes.aio.client.exceptions import ApiException

from llm_d_bench.utils.kubernetes_api import api_session, list_from_api, resolve_resource, selected_namespace

_WATCH_SECONDS = 30
_BACKOFF_INITIAL = 0.25
_BACKOFF_MAX = 5.0
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class _WatchStoppedError(Exception):
    pass


async def _until_stop(awaitable, stop: asyncio.Event | None):
    """Cancel and await in-flight work when the caller's stop event is set."""
    operation = asyncio.ensure_future(awaitable)
    stopper = asyncio.create_task(stop.wait()) if stop is not None else None
    try:
        if stopper is None:
            return await operation
        done, _ = await asyncio.wait((operation, stopper), return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return await operation
        raise _WatchStoppedError
    finally:
        for task in (operation, stopper):
            if task is not None:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def _version(document: dict) -> str:
    metadata = document.get("metadata") or {}
    version = metadata.get("resourceVersion") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError("Kubernetes watch requires resourceVersion")
    return version


async def watch_resource_events(
    resource: str,
    *,
    kubeconfig=None,
    namespace=None,
    all_namespaces=False,
    selector=None,
    stop: asyncio.Event | None = None,
):
    """Yield initial ADDED then ADDED/MODIFIED/DELETED events with raw objects.

    RESET (object=None) invalidates the previous snapshot after HTTP/event 410;
    a fresh list follows. BOOKMARK only advances the resume version. Requests
    have finite timeouts, reconnects back off up to five seconds, and permission
    failures propagate. Each reconnect reloads credentials on a dedicated client,
    without occupying request-pool capacity. Use contextlib.aclosing on early exit.
    """
    if stop is not None and stop.is_set():
        return
    version = None
    backoff = _BACKOFF_INITIAL
    previous_scope = None
    try:
        while stop is None or not stop.is_set():
            try:
                # Long-lived watchers cannot lease the bounded request pool.
                # Reload credentials and create a dedicated transport per cycle.
                async with api_session(kubeconfig, pooled=False) as (api, context):
                    descriptor = await _until_stop(resolve_resource(api, resource), stop)
                    scope = None if all_namespaces else selected_namespace(context, namespace)
                    current_scope = (api.configuration.host, descriptor.path(scope), context.get("name"))
                    if previous_scope is not None and current_scope != previous_scope:
                        version = None
                        yield {"type": "RESET", "object": None}
                    previous_scope = current_scope
                    if version is None:
                        snapshot = await _until_stop(
                            list_from_api(api, descriptor, namespace=scope, selector=selector), stop
                        )
                        version = _version(snapshot)
                        for item in snapshot["items"]:
                            if stop is not None and stop.is_set():
                                return
                            yield {"type": "ADDED", "object": item}
                    query = [
                        ("watch", "true"),
                        ("allowWatchBookmarks", "true"),
                        ("resourceVersion", version),
                        ("timeoutSeconds", _WATCH_SECONDS),
                    ]
                    if selector:
                        query.append(("labelSelector", selector))
                    response = await _until_stop(
                        api.call_api(
                            descriptor.path(scope),
                            "GET",
                            query_params=query,
                            header_params={"Accept": "application/json"},
                            auth_settings=["BearerToken"],
                            _preload_content=False,
                            _return_http_data_only=True,
                            _request_timeout=ClientTimeout(
                                total=_WATCH_SECONDS + 5, connect=5, sock_read=_WATCH_SECONDS + 5
                            ),
                        ),
                        stop,
                    )
                    try:
                        if response.status != 200:
                            raise ApiException(status=response.status, reason="Kubernetes watch request failed")
                        while stop is None or not stop.is_set():
                            line = await _until_stop(response.content.readline(), stop)
                            if not line:
                                break
                            if not line.strip():
                                continue
                            event = json.loads(line)
                            if not isinstance(event, dict) or not isinstance(event.get("object"), dict):
                                raise ValueError("Invalid Kubernetes watch event")
                            kind, document = event.get("type"), event["object"]
                            if kind == "ERROR":
                                code = document.get("code")
                                if not isinstance(code, int):
                                    raise ValueError("Invalid Kubernetes watch error")
                                raise ApiException(status=code, reason="Kubernetes watch event failed")
                            if kind not in {"ADDED", "MODIFIED", "DELETED", "BOOKMARK"}:
                                raise ValueError("Invalid Kubernetes watch event type")
                            version = _version(document)
                            backoff = _BACKOFF_INITIAL
                            if kind != "BOOKMARK":
                                yield event
                    finally:
                        response.close()
            except ApiException as error:
                if error.status == 410:
                    version = None
                    yield {"type": "RESET", "object": None}
                elif error.status not in _TRANSIENT_STATUSES:
                    raise
            except (ClientError, TimeoutError):
                pass
            await _until_stop(asyncio.sleep(backoff), stop)
            backoff = min(backoff * 2, _BACKOFF_MAX)
    except _WatchStoppedError:
        return
