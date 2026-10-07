"""``evaluate_workflows`` and ``evaluate_workflow_cases`` tables.

See design doc sections 5.4.10 and 5.4.11. Workflow-level free-form fields
still round-trip through one ``extra`` JSON column, while the embedded
``cases`` array is split into a child table per rule 5 and reassembled in
saved order when converting back to a dict-like DTO.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime
from llm_d_bench.db.evaluate_persistence_models import (
    EvaluateWorkflowCaseRecord,
    EvaluateWorkflowRecord,
)


class EvaluateWorkflowRow(Base, DeclarativeDtoMixin):
    __tablename__ = "evaluate_workflows"

    dto_type = EvaluateWorkflowRecord
    column_map = {
        "kind": "kind",
        "status": "status",
        "cluster_id": "cluster_id",
        "extra": "model_extra",
        "created_at": "created_at",
        "finished_at": "finished_at",
    }

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="workflow")
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    cases: Mapped[list[EvaluateWorkflowCaseRow]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="(EvaluateWorkflowCaseRow.position, EvaluateWorkflowCaseRow.id)",
    )

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        CheckConstraint("kind = 'workflow'", name="ck_evaluate_workflows_kind_workflow"),
        Index("ix_evaluate_workflows_created_at", "created_at"),
    )

    @classmethod
    def from_dto(cls, dto: EvaluateWorkflowRecord, **extra_columns: object) -> EvaluateWorkflowRow:
        values: dict[str, object] = {
            "id": dto.id,
            "kind": dto.kind,
            "status": dto.status,
            "cluster_id": dto.cluster_id,
            "finished_at": dto.finished_at,
            "extra": dict(dto.model_extra or {}),
        }
        if dto.created_at is not None:
            values["created_at"] = dto.created_at
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: EvaluateWorkflowRecord, **extra_columns: object) -> None:
        self.kind = dto.kind
        self.status = dto.status
        self.cluster_id = dto.cluster_id
        self.finished_at = dto.finished_at
        self.extra = dict(dto.model_extra or {})
        if dto.created_at is not None:
            self.created_at = dto.created_at
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> EvaluateWorkflowRecord:
        payload: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.cluster_id is not None:
            payload["cluster_id"] = self.cluster_id
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
        if self.cases:
            payload["cases"] = [case.to_dto().to_record_dict() for case in self.cases]
        payload.update(self.extra or {})
        return EvaluateWorkflowRecord.model_validate(payload)


class EvaluateWorkflowCaseRow(Base, DeclarativeDtoMixin):
    """Child rows for ``EvaluateWorkflowRecord.cases``.

    ``position`` is an implementation-only column, not a business field from
    the design doc: it's needed to preserve the original list order exactly on
    round-trip because case ids/statuses do not encode insertion order.

    Note on ``deployment_batch_id``/``evaluation_run_id``: the design doc
    specifies ``SET NULL`` foreign keys. They're intentionally plain indexed
    columns here because Evaluate tests persist dangling synthetic deployment
    and benchmark ids onto cases (for example ``"deployment-1"`` and fixed
    UUIDs with no backing row) and expect those records to remain saveable.
    """

    __tablename__ = "evaluate_workflow_cases"

    dto_type = EvaluateWorkflowCaseRecord
    column_map = {
        "deployment_batch_id": "deployment_run_id",
        "evaluation_run_id": "evaluation_run_id",
        "status": "status",
        "cleanup_error": "cleanup_error",
        "failed_stage": "failed_stage",
        "error": "error",
        "extra": "model_extra",
        "created_at": "created_at",
        "finished_at": "finished_at",
    }

    # ``id`` (e.g. ``guide-1-1``) is only unique within its workflow, so the
    # primary key is the composite ``(workflow_id, id)``.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("evaluate_workflows.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    deployment_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    evaluation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    cleanup_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    workflow: Mapped[EvaluateWorkflowRow] = relationship(back_populates="cases")

    __mapper_args__ = {"version_id_col": version_id}

    @classmethod
    def from_dto(
        cls,
        dto: EvaluateWorkflowCaseRecord,
        **extra_columns: object,
    ) -> EvaluateWorkflowCaseRow:
        values: dict[str, object] = {
            "id": dto.id,
            "deployment_batch_id": dto.deployment_run_id,
            "evaluation_run_id": dto.evaluation_run_id,
            "status": dto.status,
            "cleanup_error": dto.cleanup_error,
            "failed_stage": dto.failed_stage,
            "error": dto.error,
            "finished_at": dto.finished_at,
            "extra": dict(dto.model_extra or {}),
        }
        if dto.created_at is not None:
            values["created_at"] = dto.created_at
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: EvaluateWorkflowCaseRecord, **extra_columns: object) -> None:
        self.deployment_batch_id = dto.deployment_run_id
        self.evaluation_run_id = dto.evaluation_run_id
        self.status = dto.status
        self.cleanup_error = dto.cleanup_error
        self.failed_stage = dto.failed_stage
        self.error = dto.error
        self.finished_at = dto.finished_at
        self.extra = dict(dto.model_extra or {})
        if dto.created_at is not None:
            self.created_at = dto.created_at
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> EvaluateWorkflowCaseRecord:
        payload: dict[str, object] = {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.deployment_batch_id is not None:
            payload["deployment_run_id"] = self.deployment_batch_id
        if self.evaluation_run_id is not None:
            payload["evaluation_run_id"] = self.evaluation_run_id
        if self.cleanup_error is not None:
            payload["cleanup_error"] = self.cleanup_error
        if self.failed_stage is not None:
            payload["failed_stage"] = self.failed_stage
        if self.error is not None:
            payload["error"] = self.error
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
        payload.update(self.extra or {})
        return EvaluateWorkflowCaseRecord.model_validate(payload)
