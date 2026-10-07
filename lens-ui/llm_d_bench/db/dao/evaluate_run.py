"""DAO for Evaluate benchmark run records."""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.evaluate_persistence_models import EvaluateRunRecord
from llm_d_bench.db.models.evaluate_run import EvaluateRunRow


class EvaluateDaoError(Exception):
    """Raised for evaluate-persistence failures that aren't domain errors."""


class EvaluateRunDao(BaseDao):
    def create(self, run: EvaluateRunRecord) -> EvaluateRunRecord:
        with self._transaction() as session:
            if session.get(EvaluateRunRow, run.id) is not None:
                raise EvaluateDaoError(f"evaluate run already exists: {run.id}")
            row = EvaluateRunRow.from_dto(run)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, run_id: str) -> EvaluateRunRecord | None:
        with self._read_only() as session:
            row = session.get(EvaluateRunRow, run_id)
            return row.to_dto() if row else None

    def list(self) -> list[EvaluateRunRecord]:
        with self._read_only() as session:
            stmt = select(EvaluateRunRow).order_by(EvaluateRunRow.created_at, EvaluateRunRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, run: EvaluateRunRecord) -> EvaluateRunRecord:
        with self._transaction() as session:
            row = session.get(EvaluateRunRow, run.id)
            if row is None:
                row = EvaluateRunRow.from_dto(run)
                session.add(row)
            else:
                row.sync_from_dto(run)
            session.flush()
            return row.to_dto()

    def delete(self, run_id: str) -> None:
        with self._transaction() as session:
            row = session.get(EvaluateRunRow, run_id)
            if row is not None:
                session.delete(row)
