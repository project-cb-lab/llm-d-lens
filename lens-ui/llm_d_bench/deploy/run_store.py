"""Durable storage for Deploy-owned configuration runs and executions.

Backed by SQLAlchemy repositories instead of JSON files. ``base_dir`` is
accepted but ignored so the many existing tests/call sites constructing
``JsonDeploymentRunStore(tmp_path)`` keep working unchanged; per-test
isolation now comes from the database fixture in the repo root
``conftest.py`` rather than a per-instance directory.
"""

from __future__ import annotations

from pathlib import Path

from llm_d_bench.db.dao.deployment_batch import (
    DeploymentBatchDao,
    DeploymentDaoError,
    DeploymentEvidenceDao,
)
from llm_d_bench.deploy.contracts import (
    DeploymentCase,
    DeploymentExecution,
    DeploymentMetadata,
    DeploymentMetadataUpdateRequest,
    DeploymentRun,
)


class DeploymentRunStoreError(DeploymentDaoError):
    """Base error for the Deploy run store (alias of repository failures)."""


class JsonDeploymentRunStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        del base_dir  # unused -- see module docstring
        self._runs = DeploymentBatchDao()
        self._executions = DeploymentEvidenceDao()

    def create_run(self, run: DeploymentRun) -> DeploymentRun:
        try:
            return self._runs.create(run)
        except DeploymentDaoError as error:
            raise DeploymentRunStoreError(str(error)) from error

    def get_run(self, run_id: str) -> DeploymentRun | None:
        return self._runs.get(run_id)

    def list_runs(self) -> list[DeploymentRun]:
        return self._runs.list()

    def save_run(self, run: DeploymentRun) -> DeploymentRun:
        try:
            return self._runs.save(run)
        except DeploymentDaoError as error:
            raise DeploymentRunStoreError(str(error)) from error

    def get_case(self, run_id: str, case_id: str) -> DeploymentCase | None:
        return self._runs.get_case(run_id, case_id)

    def save_execution(self, execution: DeploymentExecution, *, preserve_metadata: bool = True) -> DeploymentExecution:
        """Persist a deployment execution produced by lifecycle code.

        Lifecycle writers rebuild an execution from provider output and never
        own user metadata, so the persisted metadata is carried forward unless a
        caller explicitly takes ownership of it.
        """
        try:
            return self._executions.save(execution, preserve_metadata=preserve_metadata)
        except DeploymentDaoError as error:
            raise DeploymentRunStoreError(str(error)) from error

    def update_execution_metadata(
        self, execution_id: str, request: DeploymentMetadataUpdateRequest
    ) -> DeploymentExecution:
        """Apply an allowlisted metadata patch as one locked read-modify-write."""
        try:
            return self._executions.update_metadata(execution_id, request)
        except DeploymentDaoError as error:
            raise DeploymentRunStoreError(str(error)) from error

    def get_execution(self, execution_id: str) -> DeploymentExecution | None:
        execution = self._executions.get(execution_id)
        if execution is None:
            return None
        if not hasattr(execution, "metadata") or execution.metadata is None:
            execution.metadata = DeploymentMetadata()
        return execution

    def list_executions(self) -> list[DeploymentExecution]:
        return self._executions.list()

    def delete_execution(self, execution_id: str) -> None:
        """Remove one execution record without touching its sibling executions."""
        try:
            self._executions.delete(execution_id)
        except DeploymentDaoError as error:
            raise DeploymentRunStoreError(str(error)) from error

    def delete_run(self, run_id: str) -> None:
        try:
            self._runs.delete(run_id)
        except DeploymentDaoError as error:
            raise DeploymentRunStoreError(str(error)) from error
