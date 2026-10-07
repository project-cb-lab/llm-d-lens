"""``model_cache_entries`` table -- see design doc section 5.4.3.

Backs ``llm_d_bench.model_cache.contracts.ModelCacheEntry``. ``source``
(``ModelSource``) and ``token_source`` (``TokenSource``) are expanded into
prefixed columns per rule 2 (section 5.2); ``node_progress``
(``list[NodeDownloadStatus]``) is a detail snapshot that isn't queried
independently, so it's stored whole as a JSONVariant array per rule 6.
``source_display`` is a derived column (equivalent to
``ModelSource.display_name()``) kept in sync on every write so the
``(storage_volume_id, source_display)`` unique index can replace the old
``find_by_volume_and_source`` full-table scan.

Note on ``cluster_id``/``storage_volume_id``: the design doc specifies both
as real foreign keys (``RESTRICT``). They're intentionally left as plain
indexed columns here, for the same reason already documented on
``StorageVolumeRow``/``ConfigurationArtifactRow``: existing tests use opaque
literal ids (``"cluster-1"``, ``"vol-1"``) that aren't backed by real
persisted ``clusters``/``storage_volumes`` rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.model_cache.contracts import ModelCacheEntry, ModelSource, NodeDownloadStatus, TokenSource


class ModelCacheEntryRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_cache_entries"

    dto_type = ModelCacheEntry
    column_map = {
        "cluster_id": "cluster_id",
        "storage_volume_id": "storage_volume_id",
        "status": "status",
        "cache_path": "cache_path",
        "size_bytes": "size_bytes",
        "failure_detail": "failure_detail",
        # created_at/updated_at are caller-set (default_factory=utcnow at
        # construction, never mutated except through this repository's
        # save()), so they're preserved verbatim -- same rationale as
        # storage_volumes/ai_providers.
        "created_at": "created_at",
        "updated_at": "updated_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    storage_volume_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_huggingface_repo_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_huggingface_revision: Mapped[str | None] = mapped_column(String(255), nullable=True, default="main")
    source_model_catalog_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_display: Mapped[str] = mapped_column(Text, nullable=False)
    token_source_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    token_source_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    token_source_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    cache_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    node_progress: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False, default=list)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("uq_model_cache_volume_source", "storage_volume_id", "source_display", unique=True),)

    @classmethod
    def from_dto(cls, dto: ModelCacheEntry, **extra_columns: object) -> ModelCacheEntryRow:
        values: dict[str, object] = {col: _dget_column(dto, path) for col, path in cls.column_map.items()}
        values["id"] = dto.id
        values.update(_source_columns(dto.source))
        values.update(_token_source_columns(dto.token_source))
        values["node_progress"] = _node_progress_to_json(dto.node_progress)
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: ModelCacheEntry, **extra_columns: object) -> None:
        for col, path in self.column_map.items():
            setattr(self, col, _dget_column(dto, path))
        for col, value in _source_columns(dto.source).items():
            setattr(self, col, value)
        for col, value in _token_source_columns(dto.token_source).items():
            setattr(self, col, value)
        self.node_progress = _node_progress_to_json(dto.node_progress)
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> ModelCacheEntry:
        values: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        values["id"] = self.id
        values["source"] = _source_from_columns(self)
        values["token_source"] = {
            "mode": self.token_source_mode,
            "namespace": self.token_source_namespace,
            "name": self.token_source_name,
        }
        values["node_progress"] = self.node_progress
        return ModelCacheEntry.model_validate(values)


def _dget_column(dto: ModelCacheEntry, path: str) -> object:
    value = getattr(dto, path)
    return value.value if hasattr(value, "value") else value


def _source_columns(source: ModelSource) -> dict[str, object]:
    huggingface = source.huggingface
    model_catalog = source.model_catalog
    return {
        "source_kind": source.kind.value,
        "source_huggingface_repo_id": huggingface.repo_id if huggingface else None,
        "source_huggingface_revision": huggingface.revision if huggingface else None,
        "source_model_catalog_entry_id": model_catalog.catalog_entry_id if model_catalog else None,
        "source_display": source.display_name(),
    }


def _source_from_columns(row: ModelCacheEntryRow) -> dict[str, object]:
    huggingface = (
        {"repo_id": row.source_huggingface_repo_id, "revision": row.source_huggingface_revision}
        if row.source_kind == "huggingface"
        else None
    )
    model_catalog = (
        {"catalog_entry_id": row.source_model_catalog_entry_id} if row.source_kind == "model-catalog" else None
    )
    return {
        "kind": row.source_kind,
        "huggingface": huggingface,
        "model_catalog": model_catalog,
    }


def _token_source_columns(token_source: TokenSource) -> dict[str, object]:
    return {
        "token_source_mode": token_source.mode.value,
        "token_source_namespace": token_source.namespace,
        "token_source_name": token_source.name,
    }


def _node_progress_to_json(node_progress: list[NodeDownloadStatus]) -> list[dict[str, Any]]:
    return [status.model_dump(mode="json") for status in node_progress]
