"""``configuration_artifacts`` table -- see design doc section 5.4.5.

Backs ``llm_d_bench.configuration.models.ConfigurationArtifactRecord``. The
two nested one-to-one models (``deployable_configuration``/
``configuration_file``) are expanded into ``dc_*``/``cf_*`` prefixed columns
per rule 2 (section 5.2). ``cluster_id``/``edited_from_artifact_id`` are
*derived, queryable* columns projected out of the free-form
``dc_provenance`` JSONVariant blob (``provenance["cluster_ref"]["id"]`` /
``provenance["edited_from_artifact_id"]``) -- they aren't independent DTO
fields, so ``from_dto``/``sync_from_dto`` compute them explicitly instead of
via ``column_map``.

Note on ``cluster_id``/``edited_from_artifact_id``: the design doc specifies
these as real foreign keys (to ``clusters.id`` and this table's own
``artifact_id`` respectively, both ``ON DELETE SET NULL``). They're
intentionally left as plain indexed columns here, matching the same
deviation already made for ``storage_volumes.cluster_id`` (see that model's
docstring): existing tests use opaque literal cluster/artifact ids that
aren't backed by real persisted rows in ``clusters``/this table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.configuration.models import ConfigurationArtifactRecord, ConfigurationFile
from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.deploy.contracts import DeployableConfiguration


class ConfigurationArtifactRow(Base, DeclarativeDtoMixin):
    __tablename__ = "configuration_artifacts"

    dto_type = ConfigurationArtifactRecord
    column_map = {
        "schema_version": "schema_version",
        "revision": "revision",
        "status": "status",
        "dc_schema_version": "deployable_configuration.schema_version",
        "dc_type": "deployable_configuration.type",
        "dc_format": "deployable_configuration.format",
        "dc_content": "deployable_configuration.content",
        "dc_provider_ref": "deployable_configuration.provider_ref",
        "dc_checksum": "deployable_configuration.checksum",
        "dc_provenance": "deployable_configuration.provenance",
        "cf_type": "configuration_file.type",
        "cf_file_name": "configuration_file.file_name",
        "cf_format": "configuration_file.format",
        "cf_path": "configuration_file.path",
        "cf_size": "configuration_file.size",
        "cf_sha256": "configuration_file.sha256",
        # created_at is caller-set at construction (see service.py's
        # save_configuration, which passes datetime.now(UTC) explicitly) and
        # never mutated afterward, so it's preserved verbatim like the
        # analogous storage_volumes/ai_providers timestamps.
        "created_at": "created_at",
    }

    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="configuration-artifact.v1")
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    dc_schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="deployable-configuration.v1")
    dc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    dc_format: Mapped[str] = mapped_column(String(32), nullable=False)
    dc_content: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    dc_provider_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    dc_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    dc_provenance: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    cf_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cf_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    cf_format: Mapped[str] = mapped_column(String(32), nullable=False)
    cf_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    cf_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    edited_from_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_configuration_artifacts_created_at", "created_at"),)

    @classmethod
    def from_dto(cls, dto: ConfigurationArtifactRecord, **extra_columns: object) -> ConfigurationArtifactRow:
        values: dict[str, object] = {col: _dget(dto, path) for col, path in cls.column_map.items()}
        values["artifact_id"] = dto.artifact_id
        values.update(_derived_columns(dto.deployable_configuration.provenance))
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: ConfigurationArtifactRecord, **extra_columns: object) -> None:
        for col, path in self.column_map.items():
            setattr(self, col, _dget(dto, path))
        for col, value in _derived_columns(dto.deployable_configuration.provenance).items():
            setattr(self, col, value)
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> ConfigurationArtifactRecord:
        return ConfigurationArtifactRecord(
            artifact_id=self.artifact_id,
            schema_version=self.schema_version,
            revision=self.revision,
            status=self.status,
            deployable_configuration=DeployableConfiguration(
                schema_version=self.dc_schema_version,
                type=self.dc_type,
                format=self.dc_format,
                content=self.dc_content,
                provider_ref=self.dc_provider_ref,
                checksum=self.dc_checksum,
                provenance=self.dc_provenance,
            ),
            configuration_file=ConfigurationFile(
                type=self.cf_type,
                file_name=self.cf_file_name,
                format=self.cf_format,
                path=self.cf_path,
                size=self.cf_size,
                sha256=self.cf_sha256,
            ),
            created_at=self.created_at,
        )


def _dget(dto: ConfigurationArtifactRecord, path: str) -> object:
    obj: object = dto
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _derived_columns(provenance: dict[str, Any]) -> dict[str, object]:
    return {
        "cluster_id": provenance.get("cluster_ref", {}).get("id"),
        "edited_from_artifact_id": provenance.get("edited_from_artifact_id"),
    }
