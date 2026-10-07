"""Tests for storage volume provisioning/deletion service logic."""

from dataclasses import dataclass

import pytest

from llm_d_bench.storage import service as storage_service
from llm_d_bench.storage.contracts import (
    DynamicPvcSpec,
    LocalDiskSpec,
    NfsSpec,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumeStatus,
)
from llm_d_bench.storage.service import (
    StorageVolumeInUseError,
    StorageVolumeService,
    _pv_manifest,
    _pvc_manifest,
    compute_node_drift,
    create_volume_objects,
    delete_volume_objects,
    discover_nodes,
    discover_storage_classes,
    get_ready_volume,
)
from llm_d_bench.storage.store import StorageVolumeStore


@dataclass
class FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class FakeRunner:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[list[str]] = []

    async def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return FakeResult(0 if self.ok else 1, stderr="" if self.ok else "boom")


def _local_disk_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "capacity": "100Gi",
        "readOnly": True,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def _nfs_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "shared-cache",
        "kind": StorageVolumeKind.NFS,
        "capacity": "10Gi",
        "readOnly": False,
        "nfs": NfsSpec(server="nfs.example.com", path="/export/shared-cache"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def _dynamic_pvc_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "dynamic-cache",
        "kind": StorageVolumeKind.DYNAMIC_PVC,
        "capacity": "50Gi",
        "readOnly": True,
        "dynamicPvc": DynamicPvcSpec(storageClass="standard", namespace="llm-d-bench-storage"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def test_pv_manifest_local_disk_has_no_node_affinity_and_is_rwx():
    manifest = _pv_manifest(_local_disk_volume())
    assert "nodeAffinity" not in manifest["spec"]
    assert manifest["spec"]["hostPath"] == {"path": "/data/models", "type": "DirectoryOrCreate"}
    assert manifest["spec"]["accessModes"] == ["ReadWriteMany"]
    assert manifest["spec"]["persistentVolumeReclaimPolicy"] == "Retain"


def test_pv_manifest_nfs_has_no_node_affinity():
    manifest = _pv_manifest(_nfs_volume())
    assert "nodeAffinity" not in manifest["spec"]
    assert manifest["spec"]["nfs"] == {"server": "nfs.example.com", "path": "/export/shared-cache"}


def test_pv_manifest_dynamic_pvc_is_none():
    assert _pv_manifest(_dynamic_pvc_volume()) is None


def test_pvc_manifest_dynamic_pvc_references_storage_class():
    manifest = _pvc_manifest(_dynamic_pvc_volume())
    assert manifest["spec"]["storageClassName"] == "standard"
    assert manifest["metadata"]["namespace"] == "llm-d-bench-storage"


def test_pvc_manifest_local_disk_binds_to_static_pv():
    volume = _local_disk_volume()
    manifest = _pvc_manifest(volume)
    assert manifest["spec"]["volumeName"] == f"prism-storage-{volume.id}"
    assert manifest["spec"]["storageClassName"] == ""


@pytest.mark.asyncio
async def test_ensure_mount_in_namespace_returns_pvc_name_unchanged_when_already_home_namespace(monkeypatch):
    """No satellite is needed (and no kubectl calls happen) when the Deploy
    namespace already matches where Storage registered the volume's PVC."""
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)
    volume = _local_disk_volume(pvcName="prism-storage-abc")

    result = await storage_service.ensure_mount_in_namespace(
        volume, namespace="llm-d-bench-storage", cluster_id="cluster-1"
    )
    assert result == "prism-storage-abc"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_ensure_mount_in_namespace_creates_satellite_pv_and_pvc_for_nfs(monkeypatch):
    """Reproduces the sprocean-cluster incident: a Deploy namespace different
    from Storage's fixed namespace must get its own PV/PVC pair pointing at
    the same NFS export, or the Pod fails scheduling with
    'persistentvolumeclaim ... not found'.
    """
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _nfs_volume(pvcName="prism-storage-abc")
    result = await storage_service.ensure_mount_in_namespace(
        volume, namespace="llmd-optimized-baseline-1-1-xyz", cluster_id="cluster-1"
    )
    assert result == "prism-storage-abc"
    assert len(runner.calls) == 1
    assert runner.calls[0][:2] == ["kubectl", "apply"]


@pytest.mark.asyncio
async def test_ensure_mount_in_namespace_rejects_dynamic_pvc_cross_namespace(monkeypatch):
    """A dynamic-pvc volume's PVC is bound 1:1 to one real CSI-provisioned
    volume; mirroring it into another namespace would silently create a
    brand-new, empty volume, so this must raise instead of a Deploy that
    appears to succeed but serves an empty cache.
    """
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)
    volume = _dynamic_pvc_volume(pvcName="prism-storage-abc")

    with pytest.raises(storage_service.StorageVolumeProvisionError):
        await storage_service.ensure_mount_in_namespace(
            volume, namespace="llmd-optimized-baseline-1-1-xyz", cluster_id="cluster-1"
        )
    assert runner.calls == []


@pytest.mark.asyncio
async def test_storage_resource_statuses_nests_bound_pv_under_pvc(monkeypatch):
    volume = _local_disk_volume(id="storage-abc")

    async def fake_list_resources(resource, **kwargs):
        assert kwargs["cluster_id"] == "cluster-1"
        if resource == "persistentvolumeclaims":
            assert kwargs["all_namespaces"] is True
            return [
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-abc",
                        "namespace": "llm-d-bench-storage",
                        "labels": {"prism.ai/storage-volume-id": volume.id},
                        "creationTimestamp": "2026-09-01T00:00:00Z",
                        "uid": "pvc-uid",
                    },
                    "spec": {
                        "accessModes": ["ReadWriteMany"],
                        "volumeName": "prism-storage-storage-abc",
                        "storageClassName": "",
                        "volumeMode": "Filesystem",
                    },
                    "status": {"phase": "Bound", "capacity": {"storage": "100Gi"}},
                }
            ]
        assert kwargs["selector"] == "prism.ai/storage-volume-id,!prism.ai/storage-mount-namespace"
        return [
            {
                "kind": "PersistentVolume",
                "metadata": {
                    "name": "prism-storage-storage-abc",
                    "labels": {"prism.ai/storage-volume-id": volume.id},
                    "creationTimestamp": "2026-09-01T00:00:00Z",
                    "uid": "pv-uid",
                },
                "spec": {
                    "accessModes": ["ReadWriteMany"],
                    "capacity": {"storage": "100Gi"},
                    "claimRef": {"namespace": "llm-d-bench-storage", "name": "prism-storage-storage-abc"},
                    "persistentVolumeReclaimPolicy": "Retain",
                    "volumeMode": "Filesystem",
                    "hostPath": {"path": "/mnt/models"},
                },
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    statuses = await storage_service.storage_resource_statuses([volume])

    assert statuses == {
        volume.id: {
            "persistentVolumeClaims": [
                {
                    "name": "prism-storage-storage-abc",
                    "namespace": "llm-d-bench-storage",
                    "phase": "Bound",
                    "capacity": "100Gi",
                    "accessModes": ["ReadWriteMany"],
                    "storageClassName": None,
                    "volumeMode": "Filesystem",
                    "createdAt": "2026-09-01T00:00:00Z",
                    "uid": "pvc-uid",
                    "volumeName": "prism-storage-storage-abc",
                    "persistentVolume": {
                        "name": "prism-storage-storage-abc",
                        "namespace": None,
                        "phase": "Bound",
                        "capacity": "100Gi",
                        "accessModes": ["ReadWriteMany"],
                        "storageClassName": None,
                        "volumeMode": "Filesystem",
                        "createdAt": "2026-09-01T00:00:00Z",
                        "uid": "pv-uid",
                        "reclaimPolicy": "Retain",
                        "source": {"type": "hostPath", "detail": "/mnt/models"},
                    },
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_storage_resource_statuses_excludes_satellite_pvcs_and_pvs(monkeypatch):
    """Deployment-namespace satellites (a same-named PVC bound to a
    ``prism.ai/storage-mount-namespace``-labelled PV, see
    ``ensure_mount_in_namespace``) must never appear in the Storage table:
    only the home-namespace PVC and its own PV are this volume's resources.
    """
    volume = _nfs_volume(id="storage-abc")

    async def fake_list_resources(resource, **kwargs):
        if resource == "persistentvolumeclaims":
            return [
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-abc",
                        "namespace": "llm-d-bench-storage",
                        "labels": {"prism.ai/storage-volume-id": volume.id},
                    },
                    "spec": {"volumeName": "prism-storage-storage-abc"},
                    "status": {"phase": "Bound"},
                },
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-abc",
                        "namespace": "llmd-optimized-baseline-1-1-xyz",
                        "labels": {"prism.ai/storage-volume-id": volume.id},
                    },
                    "spec": {"volumeName": "prism-storage-storage-abc-mnt-llmd-optimized-baseline-1-1-xyz"},
                    "status": {"phase": "Bound"},
                },
            ]
        # The satellite PV selector excludes anything labelled
        # storage-mount-namespace, so kubectl would never return it here.
        return [
            {
                "kind": "PersistentVolume",
                "metadata": {"name": "prism-storage-storage-abc", "labels": {"prism.ai/storage-volume-id": volume.id}},
                "spec": {"claimRef": {"namespace": "llm-d-bench-storage", "name": "prism-storage-storage-abc"}},
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    statuses = await storage_service.storage_resource_statuses([volume])
    claims = statuses[volume.id]["persistentVolumeClaims"]

    assert len(claims) == 1
    assert claims[0]["namespace"] == "llm-d-bench-storage"
    assert claims[0]["persistentVolume"]["name"] == "prism-storage-storage-abc"


@pytest.mark.asyncio
async def test_storage_resource_statuses_isolates_cluster_failures(monkeypatch):
    """A volume whose cluster can no longer be resolved (e.g. it was
    deregistered while the volume record still references its id --
    reproduced live: ``kubeconfig_environment`` raising ``FileNotFoundError``
    for a stale ``cluster_id``) must not blank out the live status of
    volumes on other, healthy clusters requested in the same call.
    """
    healthy = _local_disk_volume(id="storage-healthy", clusterId="cluster-ok")
    stale = _local_disk_volume(id="storage-stale", clusterId="cluster-gone")

    async def fake_list_resources(resource, **kwargs):
        if kwargs["cluster_id"] == "cluster-gone":
            raise FileNotFoundError("cluster 'cluster-gone' not found")
        if resource == "persistentvolumeclaims":
            return [
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-healthy",
                        "namespace": "llm-d-bench-storage",
                        "labels": {"prism.ai/storage-volume-id": healthy.id},
                    },
                    "spec": {"volumeName": "prism-storage-storage-healthy"},
                    "status": {"phase": "Bound"},
                }
            ]
        return [
            {
                "kind": "PersistentVolume",
                "metadata": {
                    "name": "prism-storage-storage-healthy",
                    "labels": {"prism.ai/storage-volume-id": healthy.id},
                },
                "spec": {},
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    statuses = await storage_service.storage_resource_statuses([healthy, stale])

    assert len(statuses[healthy.id]["persistentVolumeClaims"]) == 1
    assert (
        statuses[healthy.id]["persistentVolumeClaims"][0]["persistentVolume"]["name"] == "prism-storage-storage-healthy"
    )
    assert statuses[stale.id]["persistentVolumeClaims"] == []


@pytest.mark.asyncio
async def test_create_volume_objects_captures_known_nodes_baseline(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "nodes"
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    volume = _local_disk_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.READY
    assert result.known_nodes == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_create_volume_objects_skips_known_nodes_for_nfs(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _nfs_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.READY
    assert result.known_nodes == []


@pytest.mark.asyncio
async def test_compute_node_drift_reports_new_nodes(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _local_disk_volume(knownNodes=["node-1"])
    drift = await compute_node_drift([volume])
    assert drift == {volume.id: ["node-2"]}


@pytest.mark.asyncio
async def test_compute_node_drift_ignores_removed_nodes(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [{"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _local_disk_volume(knownNodes=["node-1", "node-2"])
    drift = await compute_node_drift([volume])
    assert drift == {}


@pytest.mark.asyncio
async def test_compute_node_drift_skips_volumes_without_baseline(monkeypatch):
    called = False

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _local_disk_volume()  # known_nodes defaults to []
    drift = await compute_node_drift([volume])
    assert drift == {}
    assert called is False


@pytest.mark.asyncio
async def test_compute_node_drift_skips_non_local_disk_kinds(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [{"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _nfs_volume()
    drift = await compute_node_drift([volume])
    assert drift == {}


@pytest.mark.asyncio
async def test_acknowledge_nodes_rebaselines_local_disk_volume(monkeypatch, tmp_path):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume(knownNodes=["node-1"])
    store.create(volume)
    service = StorageVolumeService(store)
    updated = await service.acknowledge_nodes(volume.id)
    assert updated.known_nodes == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_create_volume_objects_marks_ready_on_success(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _local_disk_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.READY
    assert result.pvc_name == f"prism-storage-{volume.id}"
    assert result.pv_name == f"prism-storage-{volume.id}"
    assert len(runner.calls) == 1
    assert runner.calls[0][:2] == ["kubectl", "apply"]


@pytest.mark.asyncio
async def test_create_volume_objects_marks_failed_on_kubectl_error(monkeypatch):
    runner = FakeRunner(ok=False)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _local_disk_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.FAILED
    assert "boom" in (result.failure_detail or "")


@pytest.mark.asyncio
async def test_delete_volume_objects_deletes_pvc_and_pv(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)
    # delete_volume_objects also clears leftover Model Cache Jobs for the
    # volume (see delete_jobs_for_volume) via its own scoped_runner() call.
    import llm_d_bench.model_cache.jobs as model_cache_jobs

    monkeypatch.setattr(model_cache_jobs, "scoped_runner", lambda cluster_id: runner)

    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    volume.pv_name = "prism-storage-abc"
    await delete_volume_objects(volume, cluster_id="cluster-1")
    assert any(call[:3] == ["kubectl", "delete", "job"] for call in runner.calls)
    assert any(call[:3] == ["kubectl", "delete", "pvc"] for call in runner.calls)
    assert any(call[:3] == ["kubectl", "delete", "pv"] for call in runner.calls)


@pytest.mark.asyncio
async def test_delete_volume_objects_is_noop_when_cluster_no_longer_exists(monkeypatch):
    """Deleting a volume whose cluster was deregistered must not raise --
    reproduced live: deleting a ``local-disk`` volume after its cluster was
    removed surfaced ``FileNotFoundError`` from ``scoped_runner`` as a 500,
    leaving the volume permanently undeletable via the API.
    """

    def _raise_missing_cluster(cluster_id):
        raise FileNotFoundError(f"cluster {cluster_id!r} not found")

    monkeypatch.setattr(storage_service, "scoped_runner", _raise_missing_cluster)

    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    volume.pv_name = "prism-storage-abc"

    await delete_volume_objects(volume, cluster_id="cluster-gone")


@pytest.mark.asyncio
async def test_discover_storage_classes_reports_default(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "storageclasses"
        return [
            {
                "metadata": {
                    "name": "standard",
                    "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                },
                "provisioner": "kubernetes.io/aws-ebs",
            },
            {"metadata": {"name": "manual"}, "provisioner": "kubernetes.io/no-provisioner"},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    classes = await discover_storage_classes("cluster-1")
    assert {item.name: item.is_default for item in classes} == {"standard": True, "manual": False}


@pytest.mark.asyncio
async def test_discover_nodes_reports_ready(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "nodes"
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    nodes = await discover_nodes("cluster-1")
    assert {node.name: node.ready for node in nodes} == {"node-1": True, "node-2": False}
    assert all(node.schedulable for node in nodes)


@pytest.mark.asyncio
async def test_discover_nodes_reports_schedulable_from_control_plane_taints(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {
                "metadata": {"name": "control-plane-tainted"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]},
            },
            {
                "metadata": {"name": "master-tainted"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": [{"key": "node-role.kubernetes.io/master", "effect": "NoExecute"}]},
            },
            {
                "metadata": {"name": "single-node-control-plane"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": []},
            },
            {
                "metadata": {"name": "worker-1"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {},
            },
            {
                "metadata": {"name": "other-taint"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": [{"key": "some.other/taint", "effect": "NoSchedule"}]},
            },
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    nodes = await discover_nodes("cluster-1")
    assert {node.name: node.schedulable for node in nodes} == {
        "control-plane-tainted": False,
        "master-tainted": False,
        "single-node-control-plane": True,
        "worker-1": True,
        "other-taint": True,
    }


@pytest.mark.asyncio
async def test_get_ready_volume_rejects_not_ready(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    store.create(volume)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    with pytest.raises(ValueError, match="not ready"):
        await get_ready_volume(volume.id, cluster_id="cluster-1")


@pytest.mark.asyncio
async def test_get_ready_volume_rejects_wrong_cluster(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    with pytest.raises(ValueError, match="different cluster"):
        await get_ready_volume(volume.id, cluster_id="cluster-2")


@pytest.mark.asyncio
async def test_get_ready_volume_returns_ready_volume(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    resolved = await get_ready_volume(volume.id, cluster_id="cluster-1")
    assert resolved.pvc_name == "prism-storage-abc"


@pytest.mark.asyncio
async def test_service_delete_rejects_when_in_use(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: ["exec-1"])

    service = StorageVolumeService(store)
    with pytest.raises(StorageVolumeInUseError):
        await service.delete(volume.id)
    assert store.get(volume.id) is not None
    assert store.get(volume.id).status == StorageVolumeStatus.READY


@pytest.mark.asyncio
async def test_service_delete_marks_deleting_even_when_model_cache_entries_reference_it(monkeypatch, tmp_path):
    """Model Cache entries no longer block a delete -- they get cascade-deleted by ``finish_delete``."""
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: [])

    service = StorageVolumeService(store)
    updated = await service.delete(volume.id)
    assert updated.status == StorageVolumeStatus.DELETING
    assert store.get(volume.id).status == StorageVolumeStatus.DELETING


@pytest.mark.asyncio
async def test_service_delete_succeeds_when_unused(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: [])

    service = StorageVolumeService(store)
    await service.delete(volume.id)
    assert store.get(volume.id).status == StorageVolumeStatus.DELETING


@pytest.mark.asyncio
async def test_service_finish_delete_removes_volume_and_cascades_model_cache(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.DELETING
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)

    cascade_calls = []

    async def fake_delete_entries_for_volume(volume_id, *, keep_model_files):
        cascade_calls.append((volume_id, keep_model_files))

    async def _no_op_delete(volume, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "delete_model_cache_entries_for_volume", fake_delete_entries_for_volume)
    monkeypatch.setattr(storage_service, "delete_volume_objects", _no_op_delete)

    service = StorageVolumeService(store)
    await service.finish_delete(volume.id, keep_model_files=True)
    assert cascade_calls == [(volume.id, True)]
    assert store.get(volume.id) is None


@pytest.mark.asyncio
async def test_service_finish_delete_marks_failed_on_cascade_error(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.DELETING
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)

    async def _boom(volume_id, *, keep_model_files):
        raise RuntimeError("delete job failed")

    monkeypatch.setattr(storage_service, "delete_model_cache_entries_for_volume", _boom)

    service = StorageVolumeService(store)
    with pytest.raises(RuntimeError):
        await service.finish_delete(volume.id, keep_model_files=False)
    updated = store.get(volume.id)
    assert updated is not None
    assert updated.status == StorageVolumeStatus.FAILED
