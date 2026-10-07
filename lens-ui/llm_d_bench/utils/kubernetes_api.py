"""Raw Kubernetes SDK operations, independent of CLI and domain policy."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from kubernetes.aio.client.exceptions import ApiException


@dataclass(frozen=True)
class Resource:
    prefix: str
    plural: str
    namespaced: bool

    def path(self, namespace: str | None = None, name: str | None = None) -> str:
        prefix = self.prefix
        if self.namespaced and namespace:
            prefix += f"/namespaces/{quote(namespace, safe='')}"
        path = f"{prefix}/{quote(self.plural, safe='')}"
        return path + (f"/{quote(name, safe='')}" if name else "")


_BUILTINS = {}
for _prefix, _namespaced, _names in (
    (
        "/api/v1",
        True,
        "pods services secrets configmaps persistentvolumeclaims events "
        "serviceaccounts endpoints replicationcontrollers",
    ),
    ("/api/v1", False, "nodes namespaces persistentvolumes"),
    ("/apis/apps/v1", True, "deployments daemonsets statefulsets replicasets"),
    ("/apis/batch/v1", True, "jobs cronjobs"),
    ("/apis/storage.k8s.io/v1", False, "storageclasses csidrivers csinodes volumeattachments"),
    ("/apis/apiextensions.k8s.io/v1", False, "customresourcedefinitions"),
    ("/apis/networking.k8s.io/v1", True, "ingresses networkpolicies"),
    ("/apis/rbac.authorization.k8s.io/v1", True, "roles rolebindings"),
    ("/apis/rbac.authorization.k8s.io/v1", False, "clusterroles clusterrolebindings"),
):
    for _name in _names.split():
        _BUILTINS[_name] = Resource(_prefix, _name, _namespaced)

_ALIASES = {
    "po": "pods",
    "no": "nodes",
    "ns": "namespaces",
    "svc": "services",
    "cm": "configmaps",
    "pvc": "persistentvolumeclaims",
    "pv": "persistentvolumes",
    "deploy": "deployments",
    "ds": "daemonsets",
    "sts": "statefulsets",
    "rs": "replicasets",
    "sc": "storageclasses",
    "crd": "customresourcedefinitions",
    "sa": "serviceaccounts",
    "ep": "endpoints",
    "ing": "ingresses",
}
for _name in _BUILTINS:
    if _name.endswith("policies"):
        _singular = _name[:-3] + "y"
    elif _name.endswith(("classes", "ingresses")):
        _singular = _name[:-2]
    else:
        _singular = _name[:-1]
    _ALIASES[_singular] = _name


@asynccontextmanager
async def api_session(kubeconfig: str | None = None, *, pooled: bool = True):
    """Reload credentials on acquisition; pool only the compatible HTTP transport."""
    from kubernetes.aio import client

    from llm_d_bench.utils.kubernetes_auth import cleanup_configuration, load_configuration
    from llm_d_bench.utils.kubernetes_pool import pooled_client

    configuration, context = await load_configuration(kubeconfig or str(Path.home() / ".kube/config"))
    if pooled:
        async with pooled_client(configuration) as api:
            yield api, context
    else:
        try:
            async with client.ApiClient(configuration=configuration) as api:
                yield api, context
        finally:
            cleanup_configuration(configuration)


async def request(api, path: str, *, query: list | None = None, timeout: float = 10):
    return await api.call_api(
        path,
        "GET",
        query_params=query or [],
        header_params={"Accept": "application/json"},
        response_types_map={200: "object"},
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
        _request_timeout=(min(5, timeout), timeout),
    )


async def resolve_resource(api, resource: str) -> Resource:
    """Discover CRDs using the cluster's served preferred version, never guess it."""
    name, _, group = resource.partition(".")
    builtin = _BUILTINS.get(_ALIASES.get(name, name))
    if builtin:
        builtin_group = builtin.prefix.split("/")[2] if builtin.prefix.startswith("/apis/") else ""
        if not group or group == builtin_group:
            return builtin
    groups = (await request(api, "/apis")).get("groups", [])
    for entry in groups:
        if group and entry.get("name") != group:
            continue
        version = (entry.get("preferredVersion") or {}).get("groupVersion")
        if not version:
            continue
        prefix = f"/apis/{version}"
        resources = (await request(api, prefix)).get("resources", [])
        for item in resources:
            plural = item.get("name", "")
            aliases = [plural, item.get("singularName"), item.get("kind", "").lower(), *item.get("shortNames", [])]
            if "/" not in plural and name in aliases:
                return Resource(prefix, plural, bool(item.get("namespaced")))
    raise ApiException(status=404, reason="Kubernetes resource type is not served")


def selected_namespace(context: dict, namespace: str | None) -> str:
    return namespace or context["context"].get("namespace") or "default"


async def list_from_api(api, resource: Resource, *, namespace=None, selector=None, field_selector=None) -> dict:
    query = [("limit", 500)]
    if selector:
        query.append(("labelSelector", selector))
    if field_selector:
        query.append(("fieldSelector", field_selector))
    result = None
    items = []
    seen = set()
    continuation = None
    while True:
        page = await request(
            api, resource.path(namespace), query=query + ([("continue", continuation)] if continuation else [])
        )
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ValueError("Invalid Kubernetes resource list")
        if any(not isinstance(item, dict) for item in page["items"]):
            raise ValueError("Invalid Kubernetes resource item")
        if result is None:
            result = {**page}
        # Typed API lists omit each item's TypeMeta; kubectl restores it.
        # Preserve explicit item types and never infer types for a generic List.
        list_kind = page.get("kind", "")
        version = page.get("apiVersion")
        if isinstance(list_kind, str) and list_kind.endswith("List") and list_kind != "List":
            for item in page["items"]:
                item.setdefault("kind", list_kind[:-4])
                if version:
                    item.setdefault("apiVersion", version)
        items.extend(page["items"])
        metadata = page.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Invalid Kubernetes list metadata")
        continuation = metadata.get("continue")
        if not continuation:
            result["items"] = items
            result["metadata"] = {**(result.get("metadata") or {}), "continue": ""}
            result["metadata"].pop("remainingItemCount", None)
            return result
        if not isinstance(continuation, str) or continuation in seen:
            raise ValueError("Invalid Kubernetes continuation token")
        seen.add(continuation)


async def query_resource(
    resource: str,
    *,
    kubeconfig=None,
    namespace=None,
    all_namespaces=False,
    selector=None,
    field_selector=None,
    name=None,
    timeout=15,
) -> dict:
    """Strict raw get/list: preserve API failures for the caller's error contract."""
    async with asyncio.timeout(timeout), api_session(kubeconfig) as (api, context):
        descriptor = await resolve_resource(api, resource)
        scope = None if all_namespaces else selected_namespace(context, namespace)
        if name:
            if all_namespaces and descriptor.namespaced:
                raise ValueError("A named namespaced read requires a namespace")
            return await request(api, descriptor.path(scope, name))
        return await list_from_api(api, descriptor, namespace=scope, selector=selector, field_selector=field_selector)


async def server_version(*, kubeconfig=None) -> dict:
    async with asyncio.timeout(15), api_session(kubeconfig) as (api, _):
        return await request(api, "/version")
