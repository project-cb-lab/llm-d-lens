"""``monitoring_cluster_stack_operations`` table -- see design doc section 5.4.14.

Backs ``llm_d_bench.monitoring.cluster_stack.models.ClusterStackOperationResponse``.
Like the sibling accelerator-operation table, ``logs`` remains a JSONVariant
array detail stream and ``error`` is expanded into scalar columns.

Note on ``cluster_id``: the design doc specifies a real
``clusters.id`` foreign key with ``SET NULL``. It's intentionally left as a
plain indexed column here for compatibility with existing monitoring tests
and call sites, which may pass opaque literal ids not backed by persisted
``clusters`` rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime

if TYPE_CHECKING:
    from llm_d_bench.monitoring.cluster_stack.models import ClusterStackOperationResponse, OperationLogEntry


class MonitoringClusterStackOperationRow(Base, DeclarativeDtoMixin):
    __tablename__ = "monitoring_cluster_stack_operations"

    dto_type = None
    column_map = {
        "kind": "kind",
        "status": "status",
        "phase": "phase",
        "namespace": "namespace",
        "started_at": "started_at",
        "finished_at": "finished_at",
        "exit_code": "exit_code",
    }

    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="install")
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(253), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        CheckConstraint("kind = 'install'", name="ck_monitoring_cluster_stack_operations_kind_install"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_monitoring_cluster_stack_operations_status",
        ),
    )

    @classmethod
    def from_dto(
        cls, dto: ClusterStackOperationResponse, **extra_columns: object
    ) -> MonitoringClusterStackOperationRow:
        row = super().from_dto(dto, **extra_columns)
        row.operation_id = dto.operation_id
        row.logs = _logs_to_json(dto.logs)
        row.error_code = dto.error.code if dto.error is not None else None
        row.error_message = dto.error.message if dto.error is not None else None
        row.error_retryable = dto.error.retryable if dto.error is not None else None
        return row

    def sync_from_dto(self, dto: ClusterStackOperationResponse, **extra_columns: object) -> None:
        super().sync_from_dto(dto, **extra_columns)
        self.logs = _logs_to_json(dto.logs)
        self.error_code = dto.error.code if dto.error is not None else None
        self.error_message = dto.error.message if dto.error is not None else None
        self.error_retryable = dto.error.retryable if dto.error is not None else None

    def to_dto(self) -> ClusterStackOperationResponse:
        from llm_d_bench.monitoring.cluster_stack.models import ClusterStackOperationResponse

        payload: dict[str, object] = {path: getattr(self, col) for col, path in self.column_map.items()}
        payload["operation_id"] = self.operation_id
        payload["logs"] = list(self.logs)
        payload["error"] = _error_from_columns(self.error_code, self.error_message, self.error_retryable)
        return ClusterStackOperationResponse.model_validate(payload)


def _logs_to_json(logs: list[OperationLogEntry]) -> list[dict[str, Any]]:
    return [entry.model_dump(mode="json") for entry in logs]


def _error_from_columns(code: str | None, message: str | None, retryable: bool | None) -> dict[str, object] | None:
    if code is None and message is None:
        return None
    from llm_d_bench.monitoring.cluster_stack.models import OperationError

    return OperationError(code=code or "", message=message or "", retryable=bool(retryable)).model_dump(mode="json")
