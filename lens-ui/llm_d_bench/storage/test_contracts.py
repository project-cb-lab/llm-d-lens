"""Tests for storage module contracts."""

import pytest
from pydantic import ValidationError

from llm_d_bench.storage.contracts import (
    DynamicPvcSpec,
    LocalDiskSpec,
    NfsSpec,
    StorageVolumeCreateRequest,
    StorageVolumeKind,
    StorageVolumePurpose,
)


def test_local_disk_request_requires_local_disk_spec():
    with pytest.raises(ValidationError, match="requires a matching spec payload"):
        StorageVolumeCreateRequest(clusterId="cluster-1", kind=StorageVolumeKind.LOCAL_DISK, capacity="100Gi")


def test_local_disk_request_rejects_mismatched_spec():
    with pytest.raises(ValidationError, match="unexpected nfs spec"):
        StorageVolumeCreateRequest(
            clusterId="cluster-1",
            kind=StorageVolumeKind.LOCAL_DISK,
            capacity="100Gi",
            localDisk=LocalDiskSpec(hostPath="/data/models"),
            nfs=NfsSpec(server="nfs.example.com", path="/export/models"),
        )


def test_local_disk_request_accepts_matching_spec():
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        localDisk=LocalDiskSpec(hostPath="/data/models"),
    )
    assert request.local_disk.host_path == "/data/models"


def test_nfs_request_accepts_matching_spec():
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        kind=StorageVolumeKind.NFS,
        capacity="10Gi",
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared-cache"),
    )
    assert request.nfs.server == "nfs.example.com"


def test_dynamic_pvc_request_accepts_matching_spec():
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        kind=StorageVolumeKind.DYNAMIC_PVC,
        capacity="50Gi",
        dynamicPvc=DynamicPvcSpec(storageClass="standard", namespace="llm-d-bench-storage"),
    )
    assert request.dynamic_pvc.access_mode == "ReadWriteOnce"


@pytest.mark.parametrize("capacity", ["100", "100gb", "-5Gi", "100 Gi"])
def test_capacity_must_match_pattern(capacity):
    with pytest.raises(ValidationError):
        StorageVolumeCreateRequest(
            clusterId="cluster-1",
            kind=StorageVolumeKind.NFS,
            capacity=capacity,
            nfs=NfsSpec(server="nfs.example.com", path="/export/models"),
        )


def test_local_disk_spec_rejects_relative_path():
    with pytest.raises(ValidationError, match="absolute path"):
        LocalDiskSpec(hostPath="relative/path")


def test_local_disk_spec_rejects_parent_traversal():
    with pytest.raises(ValidationError, match="absolute path"):
        LocalDiskSpec(hostPath="/data/../etc")


def test_nfs_spec_rejects_relative_path():
    with pytest.raises(ValidationError, match="absolute path"):
        NfsSpec(server="nfs.example.com", path="relative")


def test_dynamic_pvc_spec_rejects_invalid_access_mode():
    with pytest.raises(ValidationError, match="accessMode"):
        DynamicPvcSpec(storageClass="standard", namespace="ns", accessMode="Bogus")


def test_purposes_default_to_empty_list():
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        kind=StorageVolumeKind.NFS,
        capacity="10Gi",
        nfs=NfsSpec(server="nfs.example.com", path="/export/models"),
    )
    assert request.purposes == []


def test_purposes_accepts_model_cache():
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        kind=StorageVolumeKind.NFS,
        capacity="10Gi",
        purposes=["model-cache"],
        nfs=NfsSpec(server="nfs.example.com", path="/export/models"),
    )
    assert request.purposes == [StorageVolumePurpose.MODEL_CACHE]


def test_model_cache_purpose_rejects_dynamic_pvc():
    with pytest.raises(ValidationError, match="model-cache purpose cannot use dynamic-pvc"):
        StorageVolumeCreateRequest(
            clusterId="cluster-1",
            kind=StorageVolumeKind.DYNAMIC_PVC,
            capacity="10Gi",
            purposes=["model-cache"],
            dynamicPvc=DynamicPvcSpec(storageClass="standard", namespace="llm-d-bench-storage"),
        )


def test_purposes_rejects_unknown_value():
    with pytest.raises(ValidationError):
        StorageVolumeCreateRequest(
            clusterId="cluster-1",
            kind=StorageVolumeKind.NFS,
            capacity="10Gi",
            purposes=["bogus"],
            nfs=NfsSpec(server="nfs.example.com", path="/export/models"),
        )
