"""Minimal Pydantic wrappers for Evaluate's free-form persisted records.

These models deliberately keep ``extra="allow"`` so
``llm_d_bench.evaluate.router`` can keep using plain dict records. Known,
queryable fields are declared explicitly for SQLAlchemy column mapping; every
other key remains in ``model_extra`` and round-trips through one JSON column
per design doc section 6.6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class EvaluateWorkflowCaseRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    deployment_run_id: str | None = None
    evaluation_run_id: str | None = None
    cleanup_error: str | None = None
    failed_stage: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None

    def to_record_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"id": self.id, "status": self.status}
        if self.deployment_run_id is not None:
            payload["deployment_run_id"] = self.deployment_run_id
        if self.evaluation_run_id is not None:
            payload["evaluation_run_id"] = self.evaluation_run_id
        if self.cleanup_error is not None:
            payload["cleanup_error"] = self.cleanup_error
        if self.failed_stage is not None:
            payload["failed_stage"] = self.failed_stage
        if self.error is not None:
            payload["error"] = self.error
        if self.created_at is not None:
            payload["created_at"] = self.created_at.isoformat()
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at.isoformat()
        payload.update(self.model_extra or {})
        return payload


class EvaluateRunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["benchmark"] = "benchmark"
    status: str
    cluster_id: str | None = None
    deployment_execution_id: str | None = None
    configuration_artifact_id: str | None = None
    created_at: datetime | None = None

    def to_record_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
        }
        if self.cluster_id is not None:
            payload["cluster_id"] = self.cluster_id
        if self.deployment_execution_id is not None:
            payload["deployment_execution_id"] = self.deployment_execution_id
        if self.configuration_artifact_id is not None:
            payload["configuration_artifact_id"] = self.configuration_artifact_id
        if self.created_at is not None:
            payload["created_at"] = self.created_at.isoformat()
        payload.update(self.model_extra or {})
        return payload


class EvaluateWorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["workflow"] = "workflow"
    status: str
    cluster_id: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    cases: list[EvaluateWorkflowCaseRecord] | None = None

    def to_record_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
        }
        if self.cluster_id is not None:
            payload["cluster_id"] = self.cluster_id
        if self.created_at is not None:
            payload["created_at"] = self.created_at.isoformat()
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at.isoformat()
        if self.cases:
            payload["cases"] = [case.to_record_dict() for case in self.cases]
        payload.update(self.model_extra or {})
        return payload
