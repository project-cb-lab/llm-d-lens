"""``deployment_evidences`` table -- see design doc section 5.4.8.

Backs ``llm_d_bench.deploy.contracts.DeploymentExecution``. Multiple nested
one-to-one structs are expanded into prefixed columns per rule 2, while
``configuration_artifacts``/``monitoring_setup``/``provenance`` remain
JSONVariant payloads per rules 3 and 6.

Note on ``cluster_id``: the design doc specifies a real ``clusters.id``
foreign key with ``SET NULL``. It's intentionally left as a plain indexed
column for the same compatibility reason documented on
``DeploymentRunRow``: current Deploy records commonly carry opaque
``cluster_server_id`` provenance values that are not guaranteed to resolve
to persisted ``clusters`` rows in tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import ArrayVariant, Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.deploy.contracts import (
    ConfigurationArtifact,
    DeploymentArtifact,
    DeploymentEndpoint,
    DeploymentExecution,
    DeploymentMetadata,
    VersionedPayload,
)


class DeploymentExecutionRow(Base, DeclarativeDtoMixin):
    __tablename__ = "deployment_evidences"

    dto_type = DeploymentExecution
    column_map = {
        "request_id": "request_id",
        "status": "status",
        "forwarded_endpoint": "forwarded_endpoint",
        "namespace": "namespace",
        "monitoring_setup": "monitoring_setup",
        "provenance": "provenance",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    artifact_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_configuration_artifact_ids: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False)
    artifact_source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artifact_manifest_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artifact_manifest_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_values_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_rendered_payload_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_rendered_payload_value: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    artifact_created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_protocol: Mapped[str | None] = mapped_column(String(16), nullable=True, default="http")
    endpoint_service_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_model_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_baseline_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    forwarded_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    monitoring_setup: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    configuration_artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False, default=list)
    resource_snapshot_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_snapshot_value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    metadata_display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    metadata_description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    evidence_refs: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False, default=list)
    diagnostics_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    diagnostics_value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("ix_deployment_evidences_created_at", "created_at"),)

    @classmethod
    def from_dto(cls, dto: DeploymentExecution, **extra_columns: object) -> DeploymentExecutionRow:
        row = super().from_dto(dto, **extra_columns)
        row.evidence_id = dto.execution_id
        _sync_artifact(row, dto.artifact)
        _sync_endpoint(row, dto.endpoint)
        row.configuration_artifacts = _artifacts_to_json(dto.configuration_artifacts)
        _sync_metadata(row, dto.metadata)
        _sync_versioned_payload(row, "resource_snapshot", dto.resource_snapshot)
        _sync_versioned_payload(row, "diagnostics", dto.diagnostics)
        row.evidence_refs = list(dto.evidence_refs)
        return row

    def sync_from_dto(self, dto: DeploymentExecution, **extra_columns: object) -> None:
        super().sync_from_dto(dto, **extra_columns)
        _sync_artifact(self, dto.artifact)
        _sync_endpoint(self, dto.endpoint)
        self.configuration_artifacts = _artifacts_to_json(dto.configuration_artifacts)
        _sync_metadata(self, dto.metadata)
        _sync_versioned_payload(self, "resource_snapshot", dto.resource_snapshot)
        _sync_versioned_payload(self, "diagnostics", dto.diagnostics)
        self.evidence_refs = list(dto.evidence_refs)

    def to_dto(self) -> DeploymentExecution:
        payload: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        payload["execution_id"] = self.evidence_id
        payload["artifact"] = _artifact_from_row(self)
        payload["endpoint"] = _endpoint_from_row(self)
        payload["configuration_artifacts"] = self.configuration_artifacts
        payload["resource_snapshot"] = _versioned_payload_from_columns(
            self.resource_snapshot_schema_version, self.resource_snapshot_value
        )
        payload["metadata"] = DeploymentMetadata(
            display_name=self.metadata_display_name,
            description=self.metadata_description,
        ).model_dump(mode="json")
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["diagnostics"] = _versioned_payload_from_columns(
            self.diagnostics_schema_version, self.diagnostics_value
        )
        return DeploymentExecution.model_validate(payload)


def _sync_artifact(row: DeploymentExecutionRow, artifact: DeploymentArtifact) -> None:
    row.artifact_artifact_id = artifact.artifact_id
    row.artifact_artifact_hash = artifact.artifact_hash
    row.artifact_configuration_artifact_ids = list(artifact.configuration_artifact_ids)
    row.artifact_source_ref = artifact.source_ref
    row.artifact_manifest_ref = artifact.manifest_ref
    row.artifact_manifest_checksum = artifact.manifest_checksum
    row.artifact_values_checksum = artifact.values_checksum
    row.artifact_rendered_payload_schema_version = artifact.rendered_payload.schema_version
    row.artifact_rendered_payload_value = artifact.rendered_payload.value
    row.artifact_created_at = artifact.created_at


def _artifact_from_row(row: DeploymentExecutionRow) -> dict[str, object]:
    return DeploymentArtifact(
        artifact_id=row.artifact_artifact_id,
        artifact_hash=row.artifact_artifact_hash,
        configuration_artifact_ids=list(row.artifact_configuration_artifact_ids),
        source_ref=row.artifact_source_ref,
        manifest_ref=row.artifact_manifest_ref,
        manifest_checksum=row.artifact_manifest_checksum,
        values_checksum=row.artifact_values_checksum,
        rendered_payload=VersionedPayload(
            schema_version=row.artifact_rendered_payload_schema_version,
            value=row.artifact_rendered_payload_value,
        ),
        created_at=row.artifact_created_at,
    ).model_dump(mode="json")


def _sync_endpoint(row: DeploymentExecutionRow, endpoint: DeploymentEndpoint | None) -> None:
    row.endpoint_url = endpoint.url if endpoint is not None else None
    row.endpoint_protocol = endpoint.protocol if endpoint is not None else None
    row.endpoint_service_ref = endpoint.service_ref if endpoint is not None else None
    row.endpoint_model_ref = endpoint.model_ref if endpoint is not None else None
    row.endpoint_baseline_url = endpoint.baseline_url if endpoint is not None else None


def _endpoint_from_row(row: DeploymentExecutionRow) -> dict[str, object] | None:
    if row.endpoint_url is None:
        return None
    return DeploymentEndpoint(
        url=row.endpoint_url,
        protocol=row.endpoint_protocol or "http",
        service_ref=row.endpoint_service_ref,
        model_ref=row.endpoint_model_ref,
        baseline_url=row.endpoint_baseline_url,
    ).model_dump(mode="json")


def _artifacts_to_json(artifacts: list[ConfigurationArtifact]) -> list[dict[str, Any]]:
    return [artifact.model_dump(mode="json") for artifact in artifacts]


def _sync_metadata(row: DeploymentExecutionRow, metadata: DeploymentMetadata) -> None:
    row.metadata_display_name = metadata.display_name
    row.metadata_description = metadata.description


def _sync_versioned_payload(row: DeploymentExecutionRow, prefix: str, payload: VersionedPayload | None) -> None:
    setattr(row, f"{prefix}_schema_version", payload.schema_version if payload is not None else None)
    setattr(row, f"{prefix}_value", payload.value if payload is not None else None)


def _versioned_payload_from_columns(
    schema_version: str | None, value: dict[str, Any] | None
) -> dict[str, object] | None:
    if schema_version is None and value is None:
        return None
    return VersionedPayload(schema_version=schema_version or "v1", value=value or {}).model_dump(mode="json")
