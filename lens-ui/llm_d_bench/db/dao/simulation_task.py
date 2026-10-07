"""DAO backing ``llm_d_bench.simulation.service`` persistence."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.simulation_task import PER_REQUEST_FILENAME, SimulationTaskRow

if TYPE_CHECKING:
    from llm_d_bench.simulation.models import SimulationTask


class SimulationTaskDaoError(Exception):
    """Raised for simulation-task repository failures that aren't domain errors."""


class SimulationTaskDao(BaseDao):
    def save(self, task: SimulationTask, upsert: bool = True) -> SimulationTask:
        with self._transaction() as session:
            row = session.get(SimulationTaskRow, task.id)
            if row is None:
                row = SimulationTaskRow.from_dto(task, result_per_request_ref=_per_request_ref(task))
                session.add(row)
            else:
                if not upsert:
                    raise SimulationTaskDaoError(f"simulation task already exists: {task.id}")
                row.sync_from_dto(task, result_per_request_ref=_per_request_ref(task))
            session.flush()
            return row.to_dto()

    def get(self, task_id: str) -> SimulationTask | None:
        with self._read_only() as session:
            row = session.get(SimulationTaskRow, task_id)
            return row.to_dto() if row else None

    def list_ids(self) -> list[str]:
        with self._read_only() as session:
            stmt = select(SimulationTaskRow.id).order_by(SimulationTaskRow.created_at, SimulationTaskRow.id)
            return list(session.scalars(stmt))

    def delete(self, task_id: str) -> None:
        with self._transaction() as session:
            row = session.get(SimulationTaskRow, task_id)
            if row is not None:
                session.delete(row)


def _per_request_ref(task: SimulationTask) -> str | None:
    if task.result is None or not task.result.per_request:
        return None
    path = Path(task.task_dir) / PER_REQUEST_FILENAME
    return str(path)
