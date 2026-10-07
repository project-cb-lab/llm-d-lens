"""Tests for the shared storage-volume mount resolver used by Deploy providers."""

import pytest

from llm_d_bench.deploy.providers.storage_mount import resolve_mount
from llm_d_bench.storage import service as storage_service
from llm_d_bench.storage.contracts import (
    LocalDiskSpec,
    NfsSpec,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumeStatus,
)


@pytest.mark.asyncio
async def test_resolve_mount_returns_none_without_storage_volume_id():
    assert await resolve_mount({}, cluster_id="cluster-1") is None
    assert await resolve_mount({"storageVolumeId": ""}, cluster_id="cluster-1") is None


@pytest.mark.asyncio
async def test_resolve_mount_local_disk_read_only_has_no_node_pinning(monkeypatch):
    volume = StorageVolume(
        clusterId="cluster-1",
        name="qwen-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.READY,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
        pvcName="prism-storage-abc",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        assert volume_id == volume.id
        assert cluster_id == "cluster-1"
        return volume

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)

    result = await resolve_mount({"storageVolumeId": volume.id}, cluster_id="cluster-1")
    assert result == {
        "volume_source": {"persistentVolumeClaim": {"claimName": "prism-storage-abc"}},
        "mount_path": "/model-cache",
        "read_only": True,
        "model_source": "shared-path",
    }


@pytest.mark.asyncio
async def test_resolve_mount_nfs_writable(monkeypatch):
    volume = StorageVolume(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared"),
        pvcName="prism-storage-def",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)

    result = await resolve_mount({"storageVolumeId": volume.id}, cluster_id="cluster-1")
    assert result["model_source"] == "auto-cache"
    assert result["read_only"] is False


@pytest.mark.asyncio
async def test_resolve_mount_ensures_cross_namespace_pvc_when_namespace_given(monkeypatch):
    """Reproduces the sprocean-cluster incident: without threading the actual
    Deploy namespace through, the returned claimName pointed at a PVC that
    only exists in Storage's fixed namespace, so the rendered Pod failed
    scheduling with 'persistentvolumeclaim ... not found' in its own (a
    different, per-run) namespace.
    """
    volume = StorageVolume(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared"),
        pvcName="prism-storage-def",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    ensure_calls = []

    async def fake_ensure_mount_in_namespace(vol, *, namespace, cluster_id):
        ensure_calls.append((vol.id, namespace, cluster_id))
        return vol.pvc_name

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(storage_service, "ensure_mount_in_namespace", fake_ensure_mount_in_namespace)

    result = await resolve_mount(
        {"storageVolumeId": volume.id}, cluster_id="cluster-1", namespace="llmd-optimized-baseline-1-1-xyz"
    )
    assert ensure_calls == [(volume.id, "llmd-optimized-baseline-1-1-xyz", "cluster-1")]
    assert result["volume_source"] == {"persistentVolumeClaim": {"claimName": "prism-storage-def"}}


@pytest.mark.asyncio
async def test_resolve_mount_skips_namespace_resolution_when_namespace_not_given(monkeypatch):
    """Backward-compat: callers that don't yet thread a namespace through
    (or tests) keep getting the volume's own PVC name directly."""
    volume = StorageVolume(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared"),
        pvcName="prism-storage-def",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("ensure_mount_in_namespace should not be called without a namespace")

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(storage_service, "ensure_mount_in_namespace", fail_if_called)

    result = await resolve_mount({"storageVolumeId": volume.id}, cluster_id="cluster-1")
    assert result["volume_source"] == {"persistentVolumeClaim": {"claimName": "prism-storage-def"}}
