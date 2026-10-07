"""Repositories backing ``llm_d_bench.deploy.run_store``."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.deployment_batch import DeploymentRunRow
from llm_d_bench.db.models.deployment_evidence import DeploymentExecutionRow
from llm_d_bench.db.models.deployment_job import DeploymentCaseRow
from llm_d_bench.deploy.contracts import (
    DeploymentExecution,
    DeploymentMetadata,
    DeploymentMetadataUpdateRequest,
    DeploymentRun,
    utcnow,
)


class DeploymentDaoError(Exception):
    """Raised for deployment-repository failures that aren't already domain errors."""


class DeploymentBatchDao(BaseDao):
    def create(self, run: DeploymentRun) -> DeploymentRun:
        with self._transaction() as session:
            if session.get(DeploymentRunRow, run.id) is not None:
                raise DeploymentDaoError(f"deployment run already exists: {run.id}")
            row = DeploymentRunRow.from_dto(run, cluster_id=_resolve_cluster_id(run.provenance))
            row.jobs = [DeploymentCaseRow.from_dto(case, batch_id=run.id) for case in run.cases]
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, run_id: str) -> DeploymentRun | None:
        with self._read_only() as session:
            stmt = (
                select(DeploymentRunRow)
                .options(selectinload(DeploymentRunRow.jobs))
                .where(DeploymentRunRow.id == run_id)
            )
            row = session.scalars(stmt).one_or_none()
            return row.to_dto() if row else None

    def list(self) -> list[DeploymentRun]:
        with self._read_only() as session:
            stmt = (
                select(DeploymentRunRow)
                .options(selectinload(DeploymentRunRow.jobs))
                .order_by(DeploymentRunRow.created_at, DeploymentRunRow.id)
            )
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, run: DeploymentRun) -> DeploymentRun:
        with self._transaction() as session:
            stmt = (
                select(DeploymentRunRow)
                .options(selectinload(DeploymentRunRow.jobs))
                .where(DeploymentRunRow.id == run.id)
            )
            row = session.scalars(stmt).one_or_none()
            if row is None:
                raise DeploymentDaoError(f"deployment run not found: {run.id}")
            row.sync_from_dto(run, cluster_id=_resolve_cluster_id(run.provenance))
            _sync_jobs_collection(row, run)
            session.flush()
            return row.to_dto()

    def set_owner(self, run_id: str, *, owner_user_id: str, owner_group_id: str | None = None) -> None:
        with self._transaction() as session:
            row = session.get(DeploymentRunRow, run_id)
            if row is None:
                raise DeploymentDaoError(f"deployment run not found: {run_id}")
            row.owner_user_id = owner_user_id
            row.owner_group_id = owner_group_id

    def owner_map(self, run_ids) -> dict[str, tuple[str | None, str | None]]:
        ids = [run_id for run_id in set(run_ids) if run_id]
        if not ids:
            return {}
        with self._read_only() as session:
            stmt = select(DeploymentRunRow.id, DeploymentRunRow.owner_user_id, DeploymentRunRow.owner_group_id).where(
                DeploymentRunRow.id.in_(ids)
            )
            return {row[0]: (row[1], row[2]) for row in session.execute(stmt)}

    def get_case(self, run_id: str, case_id: str):
        with self._read_only() as session:
            stmt = select(DeploymentCaseRow).where(
                DeploymentCaseRow.batch_id == run_id,
                DeploymentCaseRow.id == case_id,
            )
            row = session.scalars(stmt).one_or_none()
            return row.to_dto() if row else None

    def delete(self, run_id: str) -> None:
        with self._transaction() as session:
            stmt = (
                select(DeploymentRunRow)
                .options(selectinload(DeploymentRunRow.jobs))
                .where(DeploymentRunRow.id == run_id)
            )
            row = session.scalars(stmt).one_or_none()
            if row is None:
                raise DeploymentDaoError(f"deployment run not found: {run_id}")
            execution_ids = [job.evidence_id for job in row.jobs if job.evidence_id]
            session.delete(row)
            if execution_ids:
                session.execute(
                    delete(DeploymentExecutionRow).where(DeploymentExecutionRow.evidence_id.in_(execution_ids))
                )


class DeploymentEvidenceDao(BaseDao):
    def get(self, execution_id: str) -> DeploymentExecution | None:
        with self._read_only() as session:
            row = session.get(DeploymentExecutionRow, execution_id)
            return row.to_dto() if row else None

    def list(self) -> list[DeploymentExecution]:
        with self._read_only() as session:
            stmt = select(DeploymentExecutionRow).order_by(
                DeploymentExecutionRow.created_at, DeploymentExecutionRow.evidence_id
            )
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, execution: DeploymentExecution, *, preserve_metadata: bool = True) -> DeploymentExecution:
        with self._transaction() as session:
            row = session.get(DeploymentExecutionRow, execution.execution_id)
            effective = _apply_preserved_execution_fields(
                execution,
                row.to_dto() if row and preserve_metadata else None,
            )
            if row is None:
                row = DeploymentExecutionRow.from_dto(effective, cluster_id=_resolve_cluster_id(effective.provenance))
                session.add(row)
            else:
                row.sync_from_dto(effective, cluster_id=_resolve_cluster_id(effective.provenance))
            session.flush()
            return row.to_dto()

    def update_metadata(self, execution_id: str, request: DeploymentMetadataUpdateRequest) -> DeploymentExecution:
        with self._transaction() as session:
            row = session.get(DeploymentExecutionRow, execution_id)
            if row is None:
                raise DeploymentDaoError(f"deployment execution not found: {execution_id}")
            current = row.to_dto()
            current.metadata = request.apply(current.metadata or DeploymentMetadata())
            current.updated_at = utcnow()
            row.sync_from_dto(current, cluster_id=_resolve_cluster_id(current.provenance))
            session.flush()
            return row.to_dto()

    def delete(self, execution_id: str) -> None:
        with self._transaction() as session:
            row = session.get(DeploymentExecutionRow, execution_id)
            if row is not None:
                session.delete(row)


def _sync_jobs_collection(row: DeploymentRunRow, run: DeploymentRun) -> None:
    existing = {job.id: job for job in row.jobs}
    incoming_ids = set()
    for case in run.cases:
        incoming_ids.add(case.id)
        job = existing.get(case.id)
        if job is None:
            row.jobs.append(DeploymentCaseRow.from_dto(case, batch_id=run.id))
            continue
        job.sync_from_dto(case, batch_id=run.id)
    for job in list(row.jobs):
        if job.id not in incoming_ids:
            row.jobs.remove(job)


def _resolve_cluster_id(provenance: dict) -> str | None:
    cluster_ref = provenance.get("cluster_ref")
    if isinstance(cluster_ref, dict) and cluster_ref.get("id"):
        return str(cluster_ref["id"])
    cluster_server_id = provenance.get("cluster_server_id")
    return str(cluster_server_id) if cluster_server_id else None


def _apply_preserved_execution_fields(
    execution: DeploymentExecution, persisted: DeploymentExecution | None
) -> DeploymentExecution:
    if persisted is None:
        return execution
    monitoring_setup = execution.monitoring_setup
    if persisted.monitoring_setup and (
        monitoring_setup is None or persisted.monitoring_setup.get("status") != "enabling"
    ):
        monitoring_setup = persisted.monitoring_setup
    return execution.model_copy(update={"metadata": persisted.metadata, "monitoring_setup": monitoring_setup})
