"""DAO for Evaluate workflow records and their child cases."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.dao.evaluate_run import EvaluateDaoError
from llm_d_bench.db.evaluate_persistence_models import EvaluateWorkflowRecord
from llm_d_bench.db.models.evaluate_workflow import EvaluateWorkflowCaseRow, EvaluateWorkflowRow


class EvaluateWorkflowDao(BaseDao):
    def create(self, workflow: EvaluateWorkflowRecord) -> EvaluateWorkflowRecord:
        with self._transaction() as session:
            stmt = (
                select(EvaluateWorkflowRow)
                .options(selectinload(EvaluateWorkflowRow.cases))
                .where(EvaluateWorkflowRow.id == workflow.id)
            )
            if session.scalars(stmt).one_or_none() is not None:
                raise EvaluateDaoError(f"evaluate workflow already exists: {workflow.id}")
            row = EvaluateWorkflowRow.from_dto(workflow)
            row.cases = _case_rows(workflow)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, workflow_id: str) -> EvaluateWorkflowRecord | None:
        with self._read_only() as session:
            stmt = (
                select(EvaluateWorkflowRow)
                .options(selectinload(EvaluateWorkflowRow.cases))
                .where(EvaluateWorkflowRow.id == workflow_id)
            )
            row = session.scalars(stmt).one_or_none()
            return row.to_dto() if row else None

    def list(self) -> list[EvaluateWorkflowRecord]:
        with self._read_only() as session:
            stmt = (
                select(EvaluateWorkflowRow)
                .options(selectinload(EvaluateWorkflowRow.cases))
                .order_by(EvaluateWorkflowRow.created_at, EvaluateWorkflowRow.id)
            )
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, workflow: EvaluateWorkflowRecord) -> EvaluateWorkflowRecord:
        with self._transaction() as session:
            stmt = (
                select(EvaluateWorkflowRow)
                .options(selectinload(EvaluateWorkflowRow.cases))
                .where(EvaluateWorkflowRow.id == workflow.id)
            )
            row = session.scalars(stmt).one_or_none()
            if row is None:
                row = EvaluateWorkflowRow.from_dto(workflow)
                row.cases = _case_rows(workflow)
                session.add(row)
            else:
                row.sync_from_dto(workflow)
                row.cases.clear()
                session.flush()
                row.cases = _case_rows(workflow)
            session.flush()
            return row.to_dto()

    def delete(self, workflow_id: str) -> None:
        with self._transaction() as session:
            stmt = (
                select(EvaluateWorkflowRow)
                .options(selectinload(EvaluateWorkflowRow.cases))
                .where(EvaluateWorkflowRow.id == workflow_id)
            )
            row = session.scalars(stmt).one_or_none()
            if row is not None:
                session.delete(row)


def _case_rows(workflow: EvaluateWorkflowRecord) -> list[EvaluateWorkflowCaseRow]:
    if workflow.cases is None:
        return []
    return [
        EvaluateWorkflowCaseRow.from_dto(case, workflow_id=workflow.id, position=position)
        for position, case in enumerate(workflow.cases)
    ]
