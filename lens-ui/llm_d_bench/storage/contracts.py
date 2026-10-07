"""Versioned input and output contracts for the storage module.

The storage module owns registration/discovery/deletion of mountable storage
volumes (local disk, NFS, dynamic PVC). It does not own the deployment
lifecycle: Deploy providers resolve a ``StorageVolume`` into a Pod
volume/volumeMount at render time (see ``deploy/providers/storage_mount.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StorageVolumeKind(StrEnum):
    LOCAL_DISK = "local-disk"
    NFS = "nfs"
    DYNAMIC_PVC = "dynamic-pvc"


class StorageVolumePurpose(StrEnum):
    """What a volume is intended to be used for.

    Optional and user-selectable at creation time; a volume may serve zero,
    one, or (in the future) multiple purposes. Today only ``model-cache`` is
    a real consumer (see ``docs/fern/pages/api-reference/model-cache.mdx``), but the
    field is a list so more purposes can be added later without a schema
    change.
    """

    MODEL_CACHE = "model-cache"


class StorageVolumeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class LocalDiskSpec(StrictModel):
    """A ``hostPath``-backed volume, expected to resolve to the *same* directory
    (same content) on every node in the cluster — e.g. an identically-named
    model cache directory pre-populated/synced on each node. This spec is
    intentionally node-agnostic: it does not pin the volume to one node, so
    Pods mounting it may be scheduled onto any node in the cluster (see
    ``docs/fern/pages/api-reference/storage.mdx``)."""

    host_path: str = Field(alias="hostPath", min_length=1)

    @model_validator(mode="after")
    def _validate_path(self):
        if (
            not self.host_path.startswith("/")
            or "\n" in self.host_path
            or "\r" in self.host_path
            or ".." in self.host_path.split("/")
        ):
            raise ValueError("hostPath must be an absolute path without '..' segments")
        return self


class NfsSpec(StrictModel):
    server: str = Field(min_length=1)
    path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_path(self):
        if not self.path.startswith("/") or "\n" in self.path or "\r" in self.path or ".." in self.path.split("/"):
            raise ValueError("path must be an absolute path without '..' segments")
        return self


class DynamicPvcSpec(StrictModel):
    storage_class: str = Field(alias="storageClass", min_length=1)
    namespace: str = Field(min_length=1)
    access_mode: str = Field(default="ReadWriteOnce", alias="accessMode")

    @model_validator(mode="after")
    def _validate_access_mode(self):
        if self.access_mode not in {"ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"}:
            raise ValueError("accessMode must be one of ReadWriteOnce, ReadWriteMany, ReadOnlyMany")
        return self


_CAPACITY_PATTERN = r"^\d+(Gi|Mi|Ti)$"


class StorageVolumeCreateRequest(StrictModel):
    cluster_id: str = Field(alias="clusterId", min_length=1)
    name: str = Field(default="", max_length=63)
    kind: StorageVolumeKind
    capacity: str = Field(pattern=_CAPACITY_PATTERN)
    read_only: bool = Field(default=True, alias="readOnly")
    purposes: list[StorageVolumePurpose] = Field(default_factory=list)
    local_disk: LocalDiskSpec | None = Field(default=None, alias="localDisk")
    nfs: NfsSpec | None = None
    dynamic_pvc: DynamicPvcSpec | None = Field(default=None, alias="dynamicPvc")

    @model_validator(mode="after")
    def _require_matching_spec(self):
        spec_by_kind = {
            StorageVolumeKind.LOCAL_DISK: self.local_disk,
            StorageVolumeKind.NFS: self.nfs,
            StorageVolumeKind.DYNAMIC_PVC: self.dynamic_pvc,
        }
        if spec_by_kind[self.kind] is None:
            raise ValueError(f"{self.kind.value} requires a matching spec payload")
        for kind, spec in spec_by_kind.items():
            if kind != self.kind and spec is not None:
                raise ValueError(f"unexpected {kind.value} spec for a {self.kind.value} storage volume")
        if self.kind == StorageVolumeKind.DYNAMIC_PVC and StorageVolumePurpose.MODEL_CACHE in self.purposes:
            raise ValueError("model-cache purpose cannot use dynamic-pvc storage volumes")
        return self


class StorageVolume(StrictModel):
    id: str = Field(default_factory=lambda: f"storage-{uuid4().hex[:8]}")
    cluster_id: str = Field(alias="clusterId")
    name: str
    kind: StorageVolumeKind
    status: StorageVolumeStatus = StorageVolumeStatus.PENDING
    capacity: str
    used_bytes: int | None = Field(default=None, alias="usedBytes")
    read_only: bool = Field(alias="readOnly")
    purposes: list[StorageVolumePurpose] = Field(default_factory=list)
    local_disk: LocalDiskSpec | None = Field(default=None, alias="localDisk")
    nfs: NfsSpec | None = None
    dynamic_pvc: DynamicPvcSpec | None = Field(default=None, alias="dynamicPvc")
    pvc_name: str | None = Field(default=None, alias="pvcName")
    pv_name: str | None = Field(default=None, alias="pvName")
    # Snapshot of ready+schedulable node names captured the moment a
    # ``local-disk`` volume last became ready (see ``discover_nodes``).
    # ``local-disk``/hostPath content is expected to be replicated
    # identically on every node (section 5.1), so this is the baseline used
    # to detect cluster scale-out/in after the fact -- see
    # ``storage/service.py::compute_node_drift``. Empty for nfs/dynamic-pvc,
    # which are network-shared and not node-scoped.
    known_nodes: list[str] = Field(default_factory=list, alias="knownNodes")
    failure_detail: str | None = Field(default=None, alias="failureDetail")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    updated_at: datetime = Field(default_factory=utcnow, alias="updatedAt")

    def api_payload(self, *, in_use_count: int = 0) -> dict[str, Any]:
        """Project the volume for HTTP clients, appending the non-persisted usage count."""
        payload = self.model_dump(mode="json", by_alias=True)
        payload["inUseCount"] = in_use_count
        return payload


class StorageClassSummary(StrictModel):
    """Slim projection of ``kubectl get storageclass`` used by the create wizard."""

    name: str
    provisioner: str
    is_default: bool = Field(default=False, alias="isDefault")


class StorageNodeSummary(StrictModel):
    """Slim projection of cluster nodes used by the local-disk create wizard."""

    name: str
    ready: bool = True
    # Whether an untolerated Pod (e.g. a Model Cache download/delete Job,
    # which sets no tolerations) can actually be scheduled onto this node.
    # A control-plane-only node (tainted NoSchedule/NoExecute and not also
    # acting as a worker) reports False here; a single-node dev cluster where
    # the control-plane taint has been removed reports True. See
    # storage/service.py::discover_nodes for how this is derived from the
    # node's ``spec.taints``.
    schedulable: bool = True
