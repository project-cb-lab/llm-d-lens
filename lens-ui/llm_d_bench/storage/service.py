"""Kubernetes object lifecycle for Storage volumes.

All ``kubectl`` calls are scoped to a cluster via
``llm_d_bench.utils.kubernetes.scoped_runner``/``list_resources``, following the
same subprocess-based convention as the rest of the codebase (no
``kubernetes`` Python client dependency). Static PV/PVC manifests follow the
precedent set by ``llm_d_bench/evaluate/router.py::_prepare_benchmark_storage``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from llm_d_bench.storage.contracts import (
    StorageClassSummary,
    StorageNodeSummary,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumeStatus,
)
from llm_d_bench.storage.store import StorageVolumeStore, StorageVolumeStoreError, default_store
from llm_d_bench.utils.kubernetes import list_resources, scoped_runner

logger = logging.getLogger(__name__)


class StorageVolumeProvisionError(Exception):
    """Raised when the underlying Kubernetes objects for a volume cannot be created."""


class StorageVolumeInUseError(Exception):
    """Raised when a volume delete is rejected because a deployment still references it.

    Model Cache entries no longer block a delete outright (see ``delete()``):
    they are cascade-deleted instead, per the caller's "keep files?" choice.
    """

    def __init__(self, execution_ids: list[str]) -> None:
        self.execution_ids = execution_ids
        super().__init__(f"storage volume is in use by {len(execution_ids)} deployment(s)")


def _pv_name(volume: StorageVolume) -> str:
    return f"prism-storage-{volume.id}"


def _pvc_name(volume: StorageVolume) -> str:
    return f"prism-storage-{volume.id}"


def _pvc_namespace(volume: StorageVolume) -> str:
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC and volume.dynamic_pvc:
        return volume.dynamic_pvc.namespace
    return "llm-d-bench-storage"


def _pv_manifest(volume: StorageVolume) -> dict | None:
    """Build the static PersistentVolume manifest for local-disk/nfs volumes.

    ``dynamic-pvc`` volumes are provisioned by a CSI driver and never get a
    Prism-managed PV, so this returns ``None`` for that kind.
    """
    pv_name = _pv_name(volume)
    namespace = _pvc_namespace(volume)
    if volume.kind == StorageVolumeKind.LOCAL_DISK:
        assert volume.local_disk is not None
        # No nodeAffinity: the hostPath directory is expected to exist with
        # identical content on every node, so Pods mounting this volume are
        # not pinned to a single node (see design doc section 5.1).
        spec: dict = {
            "hostPath": {"path": volume.local_disk.host_path, "type": "DirectoryOrCreate"},
        }
    elif volume.kind == StorageVolumeKind.NFS:
        assert volume.nfs is not None
        spec = {"nfs": {"server": volume.nfs.server, "path": volume.nfs.path}}
    else:
        return None
    spec.update(
        {
            "capacity": {"storage": volume.capacity},
            # ReadWriteMany for local-disk too: since the directory is replicated
            # per-node rather than node-pinned, Pods on different nodes may mount
            # it concurrently without a real cross-node data path.
            "accessModes": ["ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": namespace, "name": _pvc_name(volume)},
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": pv_name, "labels": {"prism.ai/storage-volume-id": volume.id}},
        "spec": spec,
    }


def _pvc_manifest(volume: StorageVolume) -> dict:
    namespace = _pvc_namespace(volume)
    spec: dict = {
        "accessModes": ["ReadWriteMany"],
        "resources": {"requests": {"storage": volume.capacity}},
    }
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC:
        assert volume.dynamic_pvc is not None
        spec["accessModes"] = [volume.dynamic_pvc.access_mode]
        spec["storageClassName"] = volume.dynamic_pvc.storage_class
    else:
        spec["storageClassName"] = ""
        spec["volumeName"] = _pv_name(volume)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _pvc_name(volume),
            "namespace": namespace,
            "labels": {"prism.ai/storage-volume-id": volume.id},
        },
        "spec": spec,
    }


async def _ensure_namespace(namespace: str, *, cluster_id: str) -> None:
    from llm_d_bench.utils.kubernetes import create_namespace, namespace_exists

    if not await namespace_exists(namespace, cluster_id=cluster_id):
        await create_namespace(namespace, cluster_id=cluster_id)


async def _kubectl_apply_list(manifests: list[dict], *, cluster_id: str) -> None:
    manifest = {"apiVersion": "v1", "kind": "List", "items": manifests}
    result = await scoped_runner(cluster_id).run(
        ["kubectl", "apply", "--filename=-", "--output=json"],
        input=json.dumps(manifest),
        timeout=30,
    )
    if not result.ok:
        raise StorageVolumeProvisionError((result.stderr or result.stdout or "kubectl apply failed").strip())


async def create_volume_objects(volume: StorageVolume, *, cluster_id: str) -> StorageVolume:
    """Create the Kubernetes objects backing a volume and return it updated in-place.

    On success ``status`` becomes ``ready`` with ``pvc_name``/``pv_name`` filled
    in; on failure ``status`` becomes ``failed`` with ``failure_detail`` set.
    Errors are swallowed into the returned volume rather than raised so callers
    running this from a background task do not need a try/except.
    """
    try:
        await _ensure_namespace(_pvc_namespace(volume), cluster_id=cluster_id)
        pv_manifest = _pv_manifest(volume)
        manifests = ([pv_manifest] if pv_manifest else []) + [_pvc_manifest(volume)]
        await _kubectl_apply_list(manifests, cluster_id=cluster_id)
    except (StorageVolumeProvisionError, FileNotFoundError, TimeoutError) as error:
        volume.status = StorageVolumeStatus.FAILED
        volume.failure_detail = str(error)[-500:]
        return volume
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = _pvc_name(volume)
    volume.pv_name = _pv_name(volume) if pv_manifest else None
    volume.failure_detail = None
    if volume.kind == StorageVolumeKind.LOCAL_DISK:
        try:
            volume.known_nodes = await _ready_schedulable_node_names(cluster_id)
        except (FileNotFoundError, TimeoutError):
            # Best-effort: a transient node-listing failure shouldn't fail
            # the volume itself, which already successfully applied its
            # PV/PVC. Node drift just can't be detected until the next
            # successful baseline capture (e.g. via acknowledge_nodes).
            volume.known_nodes = []
    return volume


def _pv_source(spec: dict) -> dict | None:
    """Summarize what a PV actually points at, for display in the Storage UI."""
    if "nfs" in spec:
        nfs = spec["nfs"] or {}
        return {"type": "nfs", "detail": f"{nfs.get('server', '')}:{nfs.get('path', '')}"}
    if "hostPath" in spec:
        return {"type": "hostPath", "detail": (spec["hostPath"] or {}).get("path", "")}
    if "csi" in spec:
        csi = spec["csi"] or {}
        return {"type": "csi", "detail": csi.get("driver", "")}
    if "local" in spec:
        return {"type": "local", "detail": (spec["local"] or {}).get("path", "")}
    return None


def _resource_status(item: dict) -> dict:
    """Project a PVC/PV Kubernetes object into the fields the Storage table needs.

    ``volumeName`` on a PVC is used by ``storage_resource_statuses`` to nest
    the matching PV inline, since PVs are cluster-scoped and multiple can
    otherwise share a Storage volume's label (satellites, filtered out
    before this point -- see ``ensure_mount_in_namespace``).
    """
    metadata = item.get("metadata") or {}
    spec = item.get("spec") or {}
    capacity = (item.get("status") or {}).get("capacity") or spec.get("capacity") or {}
    status = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace"),
        "phase": (item.get("status") or {}).get("phase") or "Unknown",
        "capacity": capacity.get("storage"),
        "accessModes": spec.get("accessModes") or [],
        "storageClassName": spec.get("storageClassName") or None,
        "volumeMode": spec.get("volumeMode"),
        "createdAt": metadata.get("creationTimestamp"),
        "uid": metadata.get("uid"),
    }
    if item.get("kind") == "PersistentVolumeClaim":
        status["volumeName"] = spec.get("volumeName")
    if item.get("kind") == "PersistentVolume":
        status["reclaimPolicy"] = spec.get("persistentVolumeReclaimPolicy")
        status["source"] = _pv_source(spec)
    return status


async def storage_resource_statuses(volumes: list[StorageVolume]) -> dict[str, dict]:
    """Return live PVC status (with its bound PV nested inline) per Storage volume.

    Only the volume's *own* PVC/PV are surfaced here: PVCs are filtered down
    to the volume's home namespace (``_pvc_namespace``) and PVs exclude the
    ``prism.ai/storage-mount-namespace``-labelled satellites created per
    deployment namespace by ``ensure_mount_in_namespace``. Those satellites
    bind a same-named-but-different-namespace PVC and are not part of what
    Storage itself manages, so listing them here would misleadingly suggest
    they back the single PVC shown for this volume.
    """
    home_namespace_by_id = {volume.id: _pvc_namespace(volume) for volume in volumes}
    statuses = {volume.id: {"persistentVolumeClaims": []} for volume in volumes}
    by_cluster: dict[str, set[str]] = {}
    for volume in volumes:
        by_cluster.setdefault(volume.cluster_id, set()).add(volume.id)

    async def collect(cluster_id: str, volume_ids: set[str]) -> None:
        # One cluster failing to resolve (e.g. its registration was removed
        # while a Storage volume still references its id, reproduced live:
        # a stale volume pointing at a deleted cluster) must not blank out
        # the live status of every *other* volume in the same request --
        # each cluster is isolated so a single bad cluster_id only leaves
        # its own volumes' statuses empty instead of failing the whole
        # ``asyncio.gather`` below with an unhandled exception.
        try:
            pvcs, pvs = await asyncio.gather(
                list_resources(
                    "persistentvolumeclaims",
                    all_namespaces=True,
                    selector="prism.ai/storage-volume-id",
                    cluster_id=cluster_id,
                ),
                list_resources(
                    "persistentvolumes",
                    # ``!prism.ai/storage-mount-namespace`` excludes satellites.
                    selector="prism.ai/storage-volume-id,!prism.ai/storage-mount-namespace",
                    cluster_id=cluster_id,
                ),
            )
        except Exception:
            return
        pvs_by_volume: dict[str, list[dict]] = {}
        for item in pvs:
            volume_id = str(((item.get("metadata") or {}).get("labels") or {}).get("prism.ai/storage-volume-id", ""))
            if volume_id in volume_ids:
                pvs_by_volume.setdefault(volume_id, []).append(_resource_status(item))
        for item in pvcs:
            volume_id = str(((item.get("metadata") or {}).get("labels") or {}).get("prism.ai/storage-volume-id", ""))
            if volume_id not in volume_ids:
                continue
            namespace = (item.get("metadata") or {}).get("namespace")
            if namespace != home_namespace_by_id.get(volume_id):
                continue
            pvc_status = _resource_status(item)
            bound_pv = next(
                (pv for pv in pvs_by_volume.get(volume_id, []) if pv["name"] == pvc_status.get("volumeName")),
                None,
            )
            pvc_status["persistentVolume"] = bound_pv
            statuses[volume_id]["persistentVolumeClaims"].append(pvc_status)

    await asyncio.gather(*(collect(cluster_id, volume_ids) for cluster_id, volume_ids in by_cluster.items()))
    return statuses


def _satellite_pv_name(volume: StorageVolume, *, namespace: str) -> str:
    # PVs are cluster-scoped, so the target namespace must be baked into the
    # name to avoid colliding with satellites created for other namespaces
    # (or with the volume's own PV, see ``_pv_name``).
    return f"prism-storage-{volume.id}-mnt-{namespace}"[:253].rstrip("-")


def _satellite_pv_manifest(volume: StorageVolume, *, namespace: str) -> dict:
    """A second static PV pointing at the exact same hostPath/nfs location as
    the volume's own PV, bound (via ``claimRef``) to a same-named PVC created
    in ``namespace``. See ``ensure_mount_in_namespace`` for why this is
    needed and why it's only safe for ``local-disk``/``nfs`` volumes.
    """
    if volume.kind == StorageVolumeKind.LOCAL_DISK:
        assert volume.local_disk is not None
        spec: dict = {"hostPath": {"path": volume.local_disk.host_path, "type": "DirectoryOrCreate"}}
    else:
        assert volume.kind == StorageVolumeKind.NFS and volume.nfs is not None
        spec = {"nfs": {"server": volume.nfs.server, "path": volume.nfs.path}}
    spec.update(
        {
            "capacity": {"storage": volume.capacity},
            "accessModes": ["ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": namespace, "name": _pvc_name(volume)},
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": _satellite_pv_name(volume, namespace=namespace),
            "labels": {"prism.ai/storage-volume-id": volume.id, "prism.ai/storage-mount-namespace": namespace},
        },
        "spec": spec,
    }


def _satellite_pvc_manifest(volume: StorageVolume, *, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _pvc_name(volume),
            "namespace": namespace,
            "labels": {"prism.ai/storage-volume-id": volume.id},
        },
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "resources": {"requests": {"storage": volume.capacity}},
            "storageClassName": "",
            "volumeName": _satellite_pv_name(volume, namespace=namespace),
        },
    }


async def ensure_mount_in_namespace(volume: StorageVolume, *, namespace: str, cluster_id: str) -> str:
    """Make ``volume`` mountable by a Pod in ``namespace``; returns the PVC name to use.

    PersistentVolumeClaims are namespace-scoped, but every Deploy provider
    runs each deployment in its own dynamically-created namespace (see
    ``deploy/service.py``'s ``namespace_factory``), which is virtually never
    the fixed namespace (``llm-d-bench-storage`` by default, see
    ``_pvc_namespace``) that the volume's own PVC/PV were registered into by
    Storage. A Pod cannot reference a PVC from a different namespace, so
    without this, any Deploy that mounts a Storage volume fails scheduling
    with "persistentvolumeclaim ... not found" (reproduced live on the
    sprocean cluster, 2026-09-04, for an ``nfs`` Model Cache volume).

    For ``local-disk``/``nfs`` volumes the backing storage (a directory
    replicated to every node, or a network share) isn't exclusively owned by
    the original PV, so a second, satellite PV pointing at the exact same
    location is created cheaply and bound to a same-named PVC in the target
    namespace (``_satellite_pv_manifest``/``_satellite_pvc_manifest``). This
    is idempotent: re-deploying to the same namespace re-applies an identical
    manifest. Cleaned up by ``delete_volume_objects`` alongside the volume's
    own PV/PVC (both share the ``prism.ai/storage-volume-id`` label).

    ``dynamic-pvc`` volumes cannot be mirrored this way: the CSI driver
    provisions one real volume exclusively for the original PVC, so a second
    PVC anywhere (even with the same StorageClass) would silently bind to a
    brand-new, empty volume rather than the cached data the user registered
    -- this raises ``StorageVolumeProvisionError`` instead of letting a
    Deploy silently mount an empty cache.
    """
    home_namespace = _pvc_namespace(volume)
    if namespace == home_namespace:
        return volume.pvc_name
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC:
        raise StorageVolumeProvisionError(
            f"storage volume {volume.id} is a dynamic-pvc volume provisioned in namespace "
            f"'{home_namespace}'; it cannot be mounted by a deployment in a different "
            f"namespace ('{namespace}') because each dynamically-provisioned PVC is bound "
            "to its own distinct underlying volume, so a second PVC would be empty."
        )
    await _ensure_namespace(namespace, cluster_id=cluster_id)
    manifests = [
        _satellite_pv_manifest(volume, namespace=namespace),
        _satellite_pvc_manifest(volume, namespace=namespace),
    ]
    await _kubectl_apply_list(manifests, cluster_id=cluster_id)
    return volume.pvc_name


async def delete_volume_objects(volume: StorageVolume, *, cluster_id: str) -> None:
    """Delete the Kubernetes objects backing a volume (idempotent, best-effort).

    ``reclaimPolicy`` is ``Retain`` (see ``_pv_manifest``) so the underlying
    data is never deleted by this call, only the PVC/PV Kubernetes objects.

    If ``cluster_id`` no longer resolves to a registered cluster (e.g. it was
    deregistered while this volume's record still referenced it, reproduced
    live: deleting a volume after its cluster was removed raised
    ``FileNotFoundError`` from deep in the ``kubectl`` plumbing and surfaced
    as a 500), there is nothing left to clean up remotely -- the volume can
    simply be forgotten so its Storage record doesn't become permanently
    undeletable.
    """
    try:
        runner = scoped_runner(cluster_id)
    except FileNotFoundError:
        return
    # Model Cache download/delete Jobs only self-clean after their 1h TTL, so
    # a volume that was ever used for Model Cache can have long-finished Jobs
    # whose (also finished, but not yet garbage-collected) Pods still show up
    # as PVC consumers -- Kubernetes then refuses to let the PVC actually
    # leave "Terminating", making this call hang until every leftover Job ages
    # out on its own (up to 1h). Clear them first so the PVC delete below can
    # complete immediately. Lazy import mirrors ``list_model_cache_refs``
    # above: Storage must not have a module-level dependency on Model Cache.
    from llm_d_bench.model_cache.jobs import delete_jobs_for_volume

    await delete_jobs_for_volume(volume, cluster_id=cluster_id)
    if volume.pvc_name:
        # Selector (not just the volume's own namespace/name) so this also
        # removes any per-namespace satellite PVCs created by
        # ``ensure_mount_in_namespace`` for cross-namespace Deploy mounts.
        await runner.run(
            [
                "kubectl",
                "delete",
                "pvc",
                "--all-namespaces",
                "--ignore-not-found=true",
                "--selector",
                f"prism.ai/storage-volume-id={volume.id}",
            ],
            timeout=30,
        )
    if volume.pv_name:
        # Selector so satellite PVs created by ``ensure_mount_in_namespace``
        # for other namespaces are cleaned up alongside the volume's own PV.
        await runner.run(
            [
                "kubectl",
                "delete",
                "pv",
                "--ignore-not-found=true",
                "--selector",
                f"prism.ai/storage-volume-id={volume.id}",
            ],
            timeout=30,
        )


async def discover_storage_classes(cluster_id: str) -> list[StorageClassSummary]:
    items = await list_resources("storageclasses", cluster_id=cluster_id)
    summaries = []
    for item in items:
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        is_default = (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            or annotations.get("storageclass.beta.kubernetes.io/is-default-class") == "true"
        )
        summaries.append(
            StorageClassSummary(
                name=str((item.get("metadata") or {}).get("name") or ""),
                provisioner=str(item.get("provisioner") or ""),
                isDefault=is_default,
            )
        )
    return [summary for summary in summaries if summary.name]


async def discover_nodes(cluster_id: str) -> list[StorageNodeSummary]:
    items = await list_resources("nodes", cluster_id=cluster_id)
    nodes = []
    for item in items:
        name = str((item.get("metadata") or {}).get("name") or "")
        if not name:
            continue
        conditions = (item.get("status") or {}).get("conditions") or []
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions)
        taints = (item.get("spec") or {}).get("taints") or []
        # A control-plane-only node is tainted NoSchedule/NoExecute so that
        # untolerated Pods never land there; single-node dev clusters (kind,
        # minikube) remove that taint so the control-plane node can also run
        # workloads ("both a control node and a worker"). Model Cache download/
        # delete Jobs set no tolerations, so a node this reports as
        # unschedulable would never actually run one -- see
        # model_cache/jobs.py::_target_nodes, which excludes these nodes.
        schedulable = not any(
            taint.get("key") in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            and taint.get("effect") in ("NoSchedule", "NoExecute")
            for taint in taints
        )
        nodes.append(StorageNodeSummary(name=name, ready=ready, schedulable=schedulable))
    return nodes


async def _ready_schedulable_node_names(cluster_id: str) -> list[str]:
    nodes = await discover_nodes(cluster_id)
    return sorted(node.name for node in nodes if node.ready and node.schedulable)


async def compute_node_drift(volumes: list[StorageVolume]) -> dict[str, list[str]]:
    """Return, per ``local-disk`` volume, cluster nodes that appeared *after*
    its ``known_nodes`` baseline was captured (see ``create_volume_objects``).

    Since a ``local-disk`` volume's hostPath content is expected to be
    identical on every node, a node that joined the cluster later has never
    had that content written to it -- Pods scheduled there would see an
    incomplete/missing cache. Volumes with no baseline yet (not ``ready``,
    or created before this field existed) are skipped rather than reported,
    since there is nothing to diff against. Node *removal* is intentionally
    not reported here: a node leaving the cluster does not make the volume
    any less usable on the nodes that remain.
    """
    candidates = [volume for volume in volumes if volume.kind == StorageVolumeKind.LOCAL_DISK and volume.known_nodes]
    if not candidates:
        return {}
    nodes_by_cluster: dict[str, list[str]] = {}
    drift: dict[str, list[str]] = {}
    for volume in candidates:
        if volume.cluster_id not in nodes_by_cluster:
            try:
                nodes_by_cluster[volume.cluster_id] = await _ready_schedulable_node_names(volume.cluster_id)
            except (FileNotFoundError, TimeoutError):
                nodes_by_cluster[volume.cluster_id] = []
        current = nodes_by_cluster[volume.cluster_id]
        added = sorted(set(current) - set(volume.known_nodes))
        if added:
            drift[volume.id] = added
    return drift


def list_execution_refs(volume_id: str) -> list[str]:
    """Return execution ids of non-terminal Deploy executions referencing a volume."""
    from llm_d_bench.deploy.executions import list_storage_volume_usage

    return list_storage_volume_usage(volume_id)


def list_model_cache_refs(volume_id: str) -> list[str]:
    """Return Model Cache entry ids that still reference a volume (see design doc section 11)."""
    from llm_d_bench.model_cache.service import list_entries_for_volume

    return [entry.id for entry in list_entries_for_volume(volume_id)]


async def delete_model_cache_entries_for_volume(volume_id: str, *, keep_model_files: bool) -> None:
    """Cascade-delete every Model Cache entry referencing a volume being deleted.

    Lazy import mirrors ``list_model_cache_refs`` above: Storage must not have
    a module-level dependency on Model Cache. ``keep_model_files=True`` drops
    each entry's record without touching files on the underlying storage;
    ``keep_model_files=False`` actually deletes the model files first, one
    entry at a time, waiting (best-effort, bounded) for each to finish.
    """
    from llm_d_bench.model_cache.service import delete_entries_for_volume

    await delete_entries_for_volume(volume_id, keep_files=keep_model_files)


async def import_existing_models_for_volume(volume: StorageVolume) -> None:
    """Auto-register Model Cache entries for model files already on a newly-ready volume.

    Lazy import mirrors ``list_model_cache_refs`` above. Best-effort: failures
    are logged and swallowed since this is a discovery convenience layered on
    top of volume creation, not something that should fail it.
    """
    from llm_d_bench.model_cache.service import import_existing_models

    try:
        created = await import_existing_models(volume)
    except Exception:
        logger.exception("event=storage_volume_model_scan_failed volume_id=%s", volume.id)
        return
    if created:
        logger.info(
            "event=storage_volume_model_scan_imported volume_id=%s count=%d",
            volume.id,
            len(created),
        )


class StorageVolumeService:
    """Facade combining the store and Kubernetes provisioning for the router."""

    def __init__(self, store: StorageVolumeStore | None = None) -> None:
        self._store = store or default_store()

    def list(self) -> list[StorageVolume]:
        return self._store.list()

    def get(self, volume_id: str) -> StorageVolume | None:
        return self._store.get(volume_id)

    def register_pending(self, volume: StorageVolume) -> StorageVolume:
        return self._store.create(volume)

    async def provision(self, volume_id: str) -> None:
        volume = self._store.get(volume_id)
        if volume is None:
            return
        volume = await create_volume_objects(volume, cluster_id=volume.cluster_id)
        self._store.save(volume)

    async def delete(self, volume_id: str) -> StorageVolume:
        """Mark a volume ``deleting`` (synchronous, fast) after checking it is unused.

        Deployments still block a delete outright -- there's no safe way to
        remove storage a running execution depends on. Model Cache entries no
        longer block it: the caller (``storage.router``) fires
        ``finish_delete`` as a background task to actually cascade-delete
        them (per the user's "keep files?" choice) before tearing down the
        volume's own Kubernetes objects, since that can take a while.
        """
        volume = self._store.get(volume_id)
        if volume is None:
            raise StorageVolumeStoreError(f"storage volume not found: {volume_id}")
        refs = list_execution_refs(volume_id)
        if refs:
            raise StorageVolumeInUseError(refs)
        volume.status = StorageVolumeStatus.DELETING
        self._store.save(volume)
        return volume

    async def finish_delete(self, volume_id: str, *, keep_model_files: bool) -> None:
        """Cascade-delete a volume's Model Cache entries, then the volume itself.

        Called as a background task by ``storage.router`` right after
        ``delete()`` synchronously flips the volume to ``deleting``.
        """
        volume = self._store.get(volume_id)
        if volume is None:
            return
        try:
            await delete_model_cache_entries_for_volume(volume_id, keep_model_files=keep_model_files)
            await delete_volume_objects(volume, cluster_id=volume.cluster_id)
        except Exception as error:
            volume.status = StorageVolumeStatus.FAILED
            volume.failure_detail = str(error)[-500:]
            self._store.save(volume)
            raise
        self._store.delete(volume_id)

    async def acknowledge_nodes(self, volume_id: str) -> StorageVolume:
        """Reset a ``local-disk`` volume's node-drift baseline to the current cluster.

        Used once an operator has manually confirmed/replicated the hostPath
        content onto whatever nodes ``compute_node_drift`` flagged as new,
        clearing the warning until the node topology changes again.
        """
        volume = self._store.get(volume_id)
        if volume is None:
            raise StorageVolumeStoreError(f"storage volume not found: {volume_id}")
        if volume.kind == StorageVolumeKind.LOCAL_DISK:
            volume.known_nodes = await _ready_schedulable_node_names(volume.cluster_id)
            self._store.save(volume)
        return volume


def default_service() -> StorageVolumeService:
    return StorageVolumeService(default_store())


async def get_ready_volume(volume_id: str, *, cluster_id: str | None = None) -> StorageVolume:
    """Look up a volume for mount resolution, raising if it is not usable.

    Used by ``deploy/providers/storage_mount.py`` at Deployment render time.
    """
    volume = default_store().get(volume_id)
    if volume is None:
        raise ValueError(f"storage volume not found: {volume_id}")
    if cluster_id and volume.cluster_id != cluster_id:
        raise ValueError(f"storage volume {volume_id} belongs to a different cluster")
    if volume.status != StorageVolumeStatus.READY or not volume.pvc_name:
        raise ValueError(f"storage volume {volume_id} is not ready (status={volume.status.value})")
    return volume
