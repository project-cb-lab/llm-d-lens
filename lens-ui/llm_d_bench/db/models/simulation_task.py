"""``simulation_tasks`` table -- see design doc section 5.4.15.

Backs ``llm_d_bench.simulation.models.SimulationTask``. The nested
``simulation``/``prompt``/``result`` models are expanded into prefixed
columns per rule 2, except for two deliberately file-backed details:

- ``result.per_request`` is potentially huge, so the database stores only its
  path reference in ``result_per_request_ref`` and ``to_dto()`` rehydrates it
  transparently from disk.
- ``SimulationResult.model_extra`` is not part of the design-doc column list;
  for backward compatibility with historical parser metadata it is preserved
  in a fixed sidecar file under ``task_dir`` and merged back into
  ``SimulationResult`` on read.

Note on ``endpoint_deployment_evidence_id`` / ``endpoint_deployment_batch_id``
 / ``endpoint_deployment_job_id`` / ``endpoint_cluster_id``: the design doc
specifies real ``SET NULL`` foreign keys. They are intentionally plain columns
here: existing Simulation tests and call sites persist opaque ids like
``"exec-1"``, ``"cluster-123"``, and ``"cluster-456"`` without first creating
backing rows in those other tables. Enforcing the FKs now would break that
existing behavior.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import ArrayVariant, Base, DeclarativeDtoMixin, JSONVariant, UTCDateTime

if TYPE_CHECKING:
    from llm_d_bench.simulation.models import SimulationResult, SimulationTask

PER_REQUEST_FILENAME = "per_request.json"
RESULT_EXTRA_FILENAME = "result_extra.json"


class SimulationTaskRow(Base, DeclarativeDtoMixin):
    __tablename__ = "simulation_tasks"

    dto_type = object
    column_map = {
        "name": "name",
        "description": "description",
        "scenario": "scenario",
        "status": "status",
        "endpoint_mode": "endpoint_mode",
        "endpoint_namespace": "endpoint_namespace",
        "endpoint_service": "endpoint_service",
        "endpoint_deployment_evidence_id": "endpoint_deployment_execution_id",
        "endpoint_deployment_batch_id": "endpoint_deployment_run_id",
        "endpoint_deployment_job_id": "endpoint_deployment_case_id",
        "endpoint_cluster_id": "endpoint_cluster_id",
        "endpoint_cluster_name": "endpoint_cluster_name",
        "endpoint_deployment_name": "endpoint_deployment_name",
        "endpoint_url": "endpoint_url",
        "model_name": "model_name",
        "simulation_backend": "simulation.backend",
        "simulation_backend_options": "simulation.backend_options",
        "simulation_duration_seconds": "simulation.duration_seconds",
        "simulation_stream": "simulation.stream",
        "simulation_grace_period_seconds": "simulation.grace_period_seconds",
        "prompt_dataset_name": "prompt.dataset.name",
        "prompt_dataset_scenario": "prompt.dataset.scenario",
        "prompt_dataset_tokenizer": "prompt.dataset.tokenizer",
        "prompt_trace_path": "prompt.trace.path",
        "prompt_trace_format": "prompt.trace.format",
        "prompt_trace_fixed_schedule": "prompt.trace.fixed_schedule",
        "prompt_trace_timeout_seconds": "prompt.trace.timeout_seconds",
        "prompt_trace_synthesis_speedup_ratio": "prompt.trace.synthesis_speedup_ratio",
        "prompt_trace_start_seconds": "prompt.trace.start_seconds",
        "prompt_trace_end_seconds": "prompt.trace.end_seconds",
        "task_dir": "task_dir",
        "progress_percent": "progress_percent",
        "progress_message": "progress_message",
        "logs": "logs",
        "error_message": "error_message",
        "owner_pid": "owner_pid",
        "owner_instance_id": "owner_instance_id",
    }

    id: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="Trace simulation")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scenario: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="external")
    endpoint_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    endpoint_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_deployment_evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    endpoint_deployment_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    endpoint_deployment_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    endpoint_cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    endpoint_cluster_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    endpoint_deployment_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    simulation_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    simulation_backend_options: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    simulation_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    simulation_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    simulation_grace_period_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_dataset_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_dataset_scenario: Mapped[str | None] = mapped_column(String(16), nullable=True)
    prompt_dataset_tokenizer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_trace_path: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_trace_format: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_trace_fixed_schedule: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    prompt_trace_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_trace_synthesis_speedup_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_trace_start_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    prompt_trace_end_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    task_dir: Mapped[str] = mapped_column(Text, nullable=False)
    progress_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    progress_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    logs: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False, default=list)
    result_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_backend: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result_backend_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    result_artifacts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONVariant, nullable=True, default=list)
    result_per_request_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_backend_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    result_warnings: Mapped[list[str] | None] = mapped_column(ArrayVariant, nullable=True, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    execution_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    owner_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        CheckConstraint(
            "scenario IN ('chat', 'api-calling', 'coding')",
            name="ck_simulation_tasks_scenario",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_simulation_tasks_status",
        ),
        CheckConstraint(
            "endpoint_mode IN ('external', 'in-cluster', 'deployment')",
            name="ck_simulation_tasks_endpoint_mode",
        ),
        CheckConstraint(
            "simulation_backend IN ('aiperf', 'trace-replayer')",
            name="ck_simulation_tasks_simulation_backend",
        ),
        CheckConstraint(
            "prompt_trace_format IN "
            "('mooncake_trace', 'bailian_trace', 'baseten_trace', 'burst_gpt_trace', 'weka_public_dataset')",
            name="ck_simulation_tasks_prompt_trace_format",
        ),
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_simulation_tasks_progress_percent",
        ),
        Index("ix_simulation_tasks_status", "status"),
        Index("ix_simulation_tasks_endpoint_cluster_id", "endpoint_cluster_id"),
        Index("ix_simulation_tasks_created_at", "created_at"),
    )

    @classmethod
    def from_dto(cls, dto: SimulationTask, **extra_columns: object) -> SimulationTaskRow:
        row = super().from_dto(dto, **extra_columns)
        row.id = dto.id
        _sync_result(row, dto.result)
        row.created_at = _parse_timestamp(dto.created_at)
        row.started_at = _parse_timestamp(dto.started_at)
        row.execution_started_at = _parse_timestamp(dto.execution_started_at)
        row.completed_at = _parse_timestamp(dto.completed_at)
        return row

    def sync_from_dto(self, dto: SimulationTask, **extra_columns: object) -> None:
        super().sync_from_dto(dto, **extra_columns)
        _sync_result(self, dto.result)
        self.created_at = _parse_timestamp(dto.created_at)
        self.started_at = _parse_timestamp(dto.started_at)
        self.execution_started_at = _parse_timestamp(dto.execution_started_at)
        self.completed_at = _parse_timestamp(dto.completed_at)

    def to_dto(self) -> SimulationTask:
        from llm_d_bench.simulation.models import SimulationTask

        payload: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "scenario": self.scenario,
            "status": self.status,
            "endpoint_mode": self.endpoint_mode,
            "endpoint_namespace": self.endpoint_namespace,
            "endpoint_service": self.endpoint_service,
            "endpoint_deployment_execution_id": self.endpoint_deployment_evidence_id,
            "endpoint_deployment_run_id": self.endpoint_deployment_batch_id,
            "endpoint_deployment_case_id": self.endpoint_deployment_job_id,
            "endpoint_cluster_id": self.endpoint_cluster_id,
            "endpoint_cluster_name": self.endpoint_cluster_name,
            "endpoint_deployment_name": self.endpoint_deployment_name,
            "endpoint_url": self.endpoint_url,
            "model_name": self.model_name,
            "simulation": {
                "backend": self.simulation_backend,
                "backend_options": self.simulation_backend_options,
                "duration_seconds": self.simulation_duration_seconds,
                "num_requests": None,
                "stream": self.simulation_stream,
                "warmup_enabled": False,
                "grace_period_seconds": self.simulation_grace_period_seconds,
            },
            "prompt": {
                "type": "trace",
                "dataset": {
                    "name": self.prompt_dataset_name,
                    "scenario": self.prompt_dataset_scenario,
                    "tokenizer": self.prompt_dataset_tokenizer,
                },
                "trace": {
                    "path": self.prompt_trace_path,
                    "format": self.prompt_trace_format,
                    "fixed_schedule": self.prompt_trace_fixed_schedule,
                    "timeout_seconds": self.prompt_trace_timeout_seconds,
                    "synthesis_speedup_ratio": self.prompt_trace_synthesis_speedup_ratio,
                    "start_seconds": self.prompt_trace_start_seconds,
                    "end_seconds": self.prompt_trace_end_seconds,
                },
            },
            "task_dir": self.task_dir,
            "progress_percent": self.progress_percent,
            "progress_message": self.progress_message,
            "logs": list(self.logs),
            "result": _result_from_row(self),
            "error_message": self.error_message,
            "created_at": _format_timestamp(self.created_at),
            "started_at": _format_timestamp(self.started_at),
            "execution_started_at": _format_timestamp(self.execution_started_at),
            "completed_at": _format_timestamp(self.completed_at),
            "owner_pid": self.owner_pid,
            "owner_instance_id": self.owner_instance_id,
        }
        return SimulationTask.model_validate(payload)


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sync_result(row: SimulationTaskRow, result: SimulationResult | None) -> None:
    if result is None:
        row.result_run_id = None
        row.result_backend = None
        row.result_backend_version = None
        row.result_summary = None
        row.result_artifacts = None
        row.result_backend_metrics = None
        row.result_warnings = None
        return
    row.result_run_id = result.run_id
    row.result_backend = result.backend
    row.result_backend_version = result.backend_version
    row.result_summary = result.summary
    row.result_artifacts = [artifact.model_dump(mode="json") for artifact in result.artifacts]
    row.result_backend_metrics = result.backend_metrics
    row.result_warnings = list(result.warnings)


def _result_from_row(row: SimulationTaskRow) -> dict[str, object] | None:
    from llm_d_bench.simulation.models import SimulationResult

    if row.result_run_id is None and row.result_backend is None:
        return None
    if row.result_run_id is None or row.result_backend is None:
        raise ValueError(f"simulation task {row.id} has an incomplete persisted result")
    payload: dict[str, object] = {
        "run_id": row.result_run_id,
        "backend": row.result_backend,
        "backend_version": row.result_backend_version,
        "summary": row.result_summary or {},
        "artifacts": list(row.result_artifacts or []),
        "per_request": _load_per_request(row.task_dir, row.result_per_request_ref),
        "backend_metrics": row.result_backend_metrics or {},
        "warnings": list(row.result_warnings or []),
    }
    payload.update(_load_result_extra(row.task_dir))
    return SimulationResult.model_validate(payload).model_dump(mode="json")


def _load_per_request(task_dir: str, ref: str | None) -> list[dict[str, Any]]:
    if ref is None:
        return []
    path = Path(ref)
    expected = Path(task_dir).resolve() / PER_REQUEST_FILENAME
    if path.resolve() != expected:
        raise ValueError(f"simulation task detail path escaped task_dir: {ref}")
    if not path.is_file():
        return []
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, list):
        raise ValueError(f"simulation task per-request detail must be a JSON array: {ref}")
    return content


def _load_result_extra(task_dir: str) -> dict[str, Any]:
    path = Path(task_dir) / RESULT_EXTRA_FILENAME
    if not path.is_file():
        return {}
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"simulation task result-extra sidecar must be a JSON object: {path}")
    return content
