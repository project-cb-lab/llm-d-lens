"""``evaluate_runs`` table -- see design doc section 5.4.9.

Backs ``llm_d_bench.db.evaluate_persistence_models.EvaluateRunRecord``. Evaluate
records remain intentionally free-form dicts at the API/business-logic layer,
so known/queryable fields are projected into dedicated columns while every
other key round-trips through ``extra`` via Pydantic ``model_extra``.

Note on ``cluster_id``/``deployment_evidence_id``/``configuration_artifact_id``:
the design doc specifies real foreign keys. They're intentionally left as
plain indexed columns here, matching existing DAL deviations elsewhere:
Evaluate tests and current callers use synthetic or not-yet-persisted ids as
opaque references, so enforcing those FKs would break compatibility.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.db.evaluate_persistence_models import EvaluateRunRecord


class EvaluateRunRow(Base, DeclarativeDtoMixin):
    __tablename__ = "evaluate_runs"

    dto_type = EvaluateRunRecord
    column_map = {
        "kind": "kind",
        "status": "status",
        "cluster_id": "cluster_id",
        "deployment_evidence_id": "deployment_execution_id",
        "configuration_artifact_id": "configuration_artifact_id",
        "extra": "model_extra",
        "created_at": "created_at",
    }

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="benchmark")
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deployment_evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    configuration_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        CheckConstraint("kind = 'benchmark'", name="ck_evaluate_runs_kind_benchmark"),
        Index("ix_evaluate_runs_created_at", "created_at"),
    )

    @classmethod
    def from_dto(cls, dto: EvaluateRunRecord, **extra_columns: object) -> EvaluateRunRow:
        values: dict[str, object] = {
            "id": dto.id,
            "kind": dto.kind,
            "status": dto.status,
            "cluster_id": dto.cluster_id,
            "deployment_evidence_id": dto.deployment_execution_id,
            "configuration_artifact_id": dto.configuration_artifact_id,
            "extra": dict(dto.model_extra or {}),
        }
        if dto.created_at is not None:
            values["created_at"] = dto.created_at
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: EvaluateRunRecord, **extra_columns: object) -> None:
        self.kind = dto.kind
        self.status = dto.status
        self.cluster_id = dto.cluster_id
        self.deployment_evidence_id = dto.deployment_execution_id
        self.configuration_artifact_id = dto.configuration_artifact_id
        self.extra = dict(dto.model_extra or {})
        if dto.created_at is not None:
            self.created_at = dto.created_at
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> EvaluateRunRecord:
        payload: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.cluster_id is not None:
            payload["cluster_id"] = self.cluster_id
        if self.deployment_evidence_id is not None:
            payload["deployment_execution_id"] = self.deployment_evidence_id
        if self.configuration_artifact_id is not None:
            payload["configuration_artifact_id"] = self.configuration_artifact_id
        payload.update(self.extra or {})
        return EvaluateRunRecord.model_validate(payload)
