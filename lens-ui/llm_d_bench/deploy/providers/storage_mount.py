"""Resolve a Deploy ``runtime.storageVolumeId`` into a Pod volume/volumeMount.

Shared by all Deploy providers (``baseline_vllm.py``,
``optimized_baseline.py``, ``precise_prefix_cache_routing.py``) so a new
storage volume type only needs a change in ``llm_d_bench/storage/service.py``,
not in every provider. See ``docs/fern/pages/api-reference/storage.mdx`` for the full design.

Returns ``None`` when ``runtime`` has no ``storageVolumeId``, so providers keep
their existing ``runtime.mountPath``/``modelSource`` hostPath handling
untouched as the backward-compatible "ad-hoc" fallback.
"""

from __future__ import annotations

from typing import Any


async def resolve_mount(
    runtime: dict[str, Any], *, cluster_id: str | None, namespace: str | None = None
) -> dict[str, Any] | None:
    """Resolve ``runtime.storageVolumeId`` into a mount descriptor, if present.

    The returned dict has keys:
      - ``volume_source``: the Pod ``volumes[]`` entry body (merged with a
        ``name`` key by the caller), e.g. ``{"persistentVolumeClaim": {...}}``.
      - ``mount_path``: container mount path (always ``/model-cache`` today).
      - ``read_only``: whether the mount should be read-only.
      - ``model_source``: ``"shared-path"`` (serve directly from the mount) or
        ``"auto-cache"`` (download to the mount so it persists across runs),
        derived from the registered volume's ``read_only`` flag.

    Unlike an earlier revision, ``local-disk`` volumes are no longer pinned to
    a single node: the registered ``hostPath`` is expected to resolve to an
    identical directory on every node in the cluster (e.g. a pre-populated
    model cache synced to all nodes), so the Pod may be scheduled anywhere.

    ``namespace`` should be the actual namespace the rendered Pod will be
    applied into (Deploy providers pass ``overrides["_deployment_namespace"]``,
    set by ``deploy/service.py`` before calling ``render()``). PVCs are
    namespace-scoped, but that namespace is virtually never the fixed one the
    volume's own PVC was registered into by Storage, so without resolving a
    namespace-local PVC here, the rendered Pod would reference a PVC that
    doesn't exist in its own namespace and fail scheduling (see
    ``storage.service.ensure_mount_in_namespace``). ``namespace`` is optional,
    defaulting to ``None``, only so existing callers/tests that don't yet
    thread it through keep working -- passing it is required to actually mount
    successfully whenever the Deploy namespace differs from Storage's.
    """
    volume_id = runtime.get("storageVolumeId")
    if not volume_id or not isinstance(volume_id, str):
        return None
    from llm_d_bench.storage.service import ensure_mount_in_namespace, get_ready_volume

    volume = await get_ready_volume(volume_id, cluster_id=cluster_id)
    read_only = volume.read_only
    pvc_name = volume.pvc_name
    if namespace:
        pvc_name = await ensure_mount_in_namespace(volume, namespace=namespace, cluster_id=cluster_id or "")
    return {
        "volume_source": {"persistentVolumeClaim": {"claimName": pvc_name}},
        "mount_path": "/model-cache",
        "read_only": read_only,
        "model_source": "shared-path" if read_only else "auto-cache",
    }
