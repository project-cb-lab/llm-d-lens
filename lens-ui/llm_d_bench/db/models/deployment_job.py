"""``deployment_jobs`` table -- see design doc section 5.4.7.

Backs ``llm_d_bench.deploy.contracts.DeploymentCase`` after
``DeploymentRun.cases`` is split into a child table per rule 5.

Note on ``evidence_id``: the design doc specifies a real foreign key to
``deployment_evidences.evidence_id`` with ``SET NULL``. It's intentionally
left as a plain indexed column here because Deploy's existing tests
explicitly persist a dangling synthetic execution id
(``"execution-vanished"``) onto a case before saving the run again, and that
behavior is relied on for delete self-healing. Enforcing the FK would break
that compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from llm_d_bench.db.base import ArrayVariant, Base, DeclarativeDtoMixin, JSONVariant
from llm_d_bench.deploy.contracts import (
    ConfigurationArtifact,
    DeploymentCase,
    DeploymentCreateRequest,
    ProblemDetails,
    VersionedPayload,
)

if TYPE_CHECKING:
    from llm_d_bench.db.models.deployment_batch import DeploymentRunRow


class DeploymentCaseRow(Base, DeclarativeDtoMixin):
    __tablename__ = "deployment_jobs"

    dto_type = DeploymentCase
    column_map = {
        "batch_id": "run_id",
        "ordinal": "ordinal",
        "source_configuration_ordinal": "source_configuration_ordinal",
        "provider_ref": "provider_ref",
        "component": "component",
        "depends_on": "depends_on",
        "status": "status",
        "attempt": "attempt",
    }

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("deployment_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_configuration_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    component: Mapped[str] = mapped_column(String(255), nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False, default=list)
    create_request_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    create_request_input_schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    create_request_configuration_artifacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONVariant, nullable=False, default=list
    )
    create_request_deployment_policy_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    create_request_deployment_policy_value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    create_request_cluster_snapshot_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    create_request_cluster_snapshot_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    create_request_cluster_snapshot_value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    create_request_provenance: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("deployment_jobs.id", ondelete="SET NULL"), nullable=True
    )
    evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    failure_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    batch: Mapped[DeploymentRunRow] = relationship(back_populates="jobs")
    parent: Mapped[DeploymentCaseRow | None] = relationship(
        remote_side=lambda: DeploymentCaseRow.id,
        back_populates="children",
        foreign_keys=lambda: [DeploymentCaseRow.parent_job_id],
    )
    children: Mapped[list[DeploymentCaseRow]] = relationship(
        back_populates="parent",
        foreign_keys=lambda: [DeploymentCaseRow.parent_job_id],
    )

    __mapper_args__ = {"version_id_col": version_id}

    @classmethod
    def from_dto(cls, dto: DeploymentCase, **extra_columns: object) -> DeploymentCaseRow:
        row = super().from_dto(dto, **extra_columns)
        row.id = dto.id
        _sync_create_request(row, dto.create_request)
        _sync_failure(row, dto.failure)
        row.parent_job_id = dto.parent_case_id
        row.evidence_id = dto.execution_id
        return row

    def sync_from_dto(self, dto: DeploymentCase, **extra_columns: object) -> None:
        super().sync_from_dto(dto, **extra_columns)
        _sync_create_request(self, dto.create_request)
        _sync_failure(self, dto.failure)
        self.parent_job_id = dto.parent_case_id
        self.evidence_id = dto.execution_id

    def to_dto(self) -> DeploymentCase:
        payload: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        payload["id"] = self.id
        payload["create_request"] = _create_request_from_row(self)
        payload["parent_case_id"] = self.parent_job_id
        payload["execution_id"] = self.evidence_id
        payload["failure"] = _failure_from_row(self)
        return DeploymentCase.model_validate(payload)


def _sync_create_request(row: DeploymentCaseRow, request: DeploymentCreateRequest) -> None:
    deployment_policy = request.deployment_policy
    cluster_snapshot = request.cluster_snapshot
    row.create_request_request_id = request.request_id
    row.create_request_input_schema_version = request.input_schema_version
    row.create_request_configuration_artifacts = _artifacts_to_json(request.configuration_artifacts)
    row.create_request_deployment_policy_schema_version = (
        deployment_policy.schema_version if deployment_policy is not None else None
    )
    row.create_request_deployment_policy_value = deployment_policy.value if deployment_policy is not None else None
    row.create_request_cluster_snapshot_ref = request.cluster_snapshot_ref
    row.create_request_cluster_snapshot_schema_version = (
        cluster_snapshot.schema_version if cluster_snapshot is not None else None
    )
    row.create_request_cluster_snapshot_value = cluster_snapshot.value if cluster_snapshot is not None else None
    row.create_request_provenance = request.provenance


def _create_request_from_row(row: DeploymentCaseRow) -> dict[str, object]:
    return {
        "request_id": row.create_request_request_id,
        "input_schema_version": row.create_request_input_schema_version,
        "configuration_artifacts": row.create_request_configuration_artifacts,
        "deployment_policy": _versioned_payload_dict(
            row.create_request_deployment_policy_schema_version,
            row.create_request_deployment_policy_value,
            default_if_missing=True,
        ),
        "cluster_snapshot_ref": row.create_request_cluster_snapshot_ref,
        "cluster_snapshot": _versioned_payload_dict(
            row.create_request_cluster_snapshot_schema_version,
            row.create_request_cluster_snapshot_value,
            default_if_missing=False,
        ),
        "provenance": row.create_request_provenance,
    }


def _artifacts_to_json(artifacts: list[ConfigurationArtifact]) -> list[dict[str, Any]]:
    return [artifact.model_dump(mode="json") for artifact in artifacts]


def _sync_failure(row: DeploymentCaseRow, failure: ProblemDetails | None) -> None:
    row.failure_status = failure.status if failure is not None else None
    row.failure_title = failure.title if failure is not None else None
    row.failure_detail = failure.detail if failure is not None else None
    row.failure_code = failure.code if failure is not None else None


def _failure_from_row(row: DeploymentCaseRow) -> dict[str, object] | None:
    if (
        row.failure_status is None
        and row.failure_title is None
        and row.failure_detail is None
        and row.failure_code is None
    ):
        return None
    return {
        "status": row.failure_status,
        "title": row.failure_title,
        "detail": row.failure_detail,
        "code": row.failure_code,
    }


def _versioned_payload_dict(
    schema_version: str | None, value: dict[str, Any] | None, *, default_if_missing: bool
) -> dict[str, object] | None:
    if schema_version is None and value is None:
        return {"schema_version": "v1", "value": {}} if default_if_missing else None
    return VersionedPayload(schema_version=schema_version or "v1", value=value or {}).model_dump(mode="json")
