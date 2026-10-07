"""``storage_volumes`` table -- see design doc section 5.4.2.

Backs ``llm_d_bench.storage.contracts.StorageVolume``. The three mutually
exclusive one-to-one specs (``local_disk``/``nfs``/``dynamic_pvc``) are
expanded into prefixed columns per rule 2 (section 5.2); the application
layer (the existing Pydantic ``model_validator`` on ``StorageVolume``/
``StorageVolumeCreateRequest``) guarantees only the group matching ``kind``
is populated. ``to_dto()`` is overridden because the generic dot-path
reconstruction in ``DeclarativeDtoMixin`` would otherwise build a non-empty
(but all-``None``) dict for the two specs that don't match ``kind``, which
fails Pydantic validation instead of round-tripping back to ``None``.

Note on ``cluster_id``: the design doc specifies this as a ``clusters.id``
foreign key with ``RESTRICT``. It's intentionally left as a plain indexed
column (no ``ForeignKey``) here: existing tests and several call sites treat
cluster ids as opaque, freely-chosen strings that aren't necessarily backed
by a persisted ``clusters`` row (e.g. ``StorageVolumeStore``/service tests
use literal ids like ``"cluster-1"``). Enforcing the FK now would break that
existing behavior; revisit once cluster-id usage across modules is made
consistent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import ArrayVariant, Base, DeclarativeDtoMixin, UTCDateTime
from llm_d_bench.storage.contracts import StorageVolume


class StorageVolumeRow(Base, DeclarativeDtoMixin):
    __tablename__ = "storage_volumes"

    dto_type = StorageVolume
    column_map = {
        "cluster_id": "cluster_id",
        "name": "name",
        "kind": "kind",
        "status": "status",
        "capacity": "capacity",
        "used_bytes": "used_bytes",
        "read_only": "read_only",
        "purposes": "purposes",
        "pvc_name": "pvc_name",
        "pv_name": "pv_name",
        "known_nodes": "known_nodes",
        "failure_detail": "failure_detail",
        # created_at/updated_at are taken verbatim from the DTO (which
        # already sets them via default_factory=utcnow at construction time)
        # rather than left to server_default -- the old file-based store
        # simply serialized the DTO as-is, and no caller mutates
        # updated_at after construction (see service.py), so this preserves
        # that byte-for-byte round trip instead of substituting a
        # DB-generated timestamp.
        "created_at": "created_at",
        "updated_at": "updated_at",
    }

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    capacity: Mapped[str] = mapped_column(String(16), nullable=False)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    purposes: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False, default=list)
    local_disk_host_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    nfs_server: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nfs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    dynamic_pvc_storage_class: Mapped[str | None] = mapped_column(String(253), nullable=True)
    dynamic_pvc_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    dynamic_pvc_access_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pvc_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    pv_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    known_nodes: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False, default=list)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("uq_storage_volumes_name", "name", unique=True),)

    @classmethod
    def from_dto(cls, dto: StorageVolume, **extra_columns: object) -> StorageVolumeRow:
        values = {col: _dget_column(dto, path) for col, path in cls.column_map.items()}
        values.update(_spec_columns(dto))
        values["id"] = dto.id
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: StorageVolume, **extra_columns: object) -> None:
        for col, path in self.column_map.items():
            setattr(self, col, _dget_column(dto, path))
        for col, value in _spec_columns(dto).items():
            setattr(self, col, value)
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> StorageVolume:
        values: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        values["id"] = self.id
        values["local_disk"] = {"host_path": self.local_disk_host_path} if self.kind == "local-disk" else None
        values["nfs"] = {"server": self.nfs_server, "path": self.nfs_path} if self.kind == "nfs" else None
        values["dynamic_pvc"] = (
            {
                "storage_class": self.dynamic_pvc_storage_class,
                "namespace": self.dynamic_pvc_namespace,
                "access_mode": self.dynamic_pvc_access_mode,
            }
            if self.kind == "dynamic-pvc"
            else None
        )
        return StorageVolume.model_validate(values)


def _dget_column(dto: StorageVolume, path: str) -> object:
    """Read a column's source attribute off the DTO, unwrapping ``Enum``s.

    ``purposes`` is a ``list[StorageVolumePurpose]``; every other mapped
    attribute here is a scalar (possibly itself an ``Enum``), so this only
    needs to handle "list of enums" as a special case.
    """
    value = getattr(dto, path)
    if isinstance(value, list):
        return [item.value if hasattr(item, "value") else item for item in value]
    return value.value if hasattr(value, "value") else value


def _spec_columns(dto: StorageVolume) -> dict[str, object]:
    local_disk = dto.local_disk
    nfs = dto.nfs
    dynamic_pvc = dto.dynamic_pvc
    return {
        "local_disk_host_path": local_disk.host_path if local_disk else None,
        "nfs_server": nfs.server if nfs else None,
        "nfs_path": nfs.path if nfs else None,
        "dynamic_pvc_storage_class": dynamic_pvc.storage_class if dynamic_pvc else None,
        "dynamic_pvc_namespace": dynamic_pvc.namespace if dynamic_pvc else None,
        "dynamic_pvc_access_mode": dynamic_pvc.access_mode if dynamic_pvc else None,
    }
