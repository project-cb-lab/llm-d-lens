"""DAO backing ``llm_d_bench.monitoring.cluster_stack.operations``."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.monitoring_cluster_stack_operation import MonitoringClusterStackOperationRow

if TYPE_CHECKING:
    from llm_d_bench.monitoring.cluster_stack.models import ClusterStackOperationResponse

_OPERATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_RECOVERY_MESSAGE = "Monitoring service restarted while the operation was running"


class MonitoringClusterStackOperationDao(BaseDao):
    def save(self, operation: ClusterStackOperationResponse, cluster_id: str | None = None) -> None:
        with self._transaction() as session:
            row = session.get(MonitoringClusterStackOperationRow, operation.operation_id)
            if row is None:
                session.add(MonitoringClusterStackOperationRow.from_dto(operation, cluster_id=cluster_id))
            else:
                row.sync_from_dto(operation, cluster_id=cluster_id)
            session.flush()

    def load(self, operation_id: str) -> ClusterStackOperationResponse | None:
        if _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            return None
        with self._read_only() as session:
            row = session.get(MonitoringClusterStackOperationRow, operation_id)
            return row.to_dto() if row else None

    def recover_interrupted(self) -> list[ClusterStackOperationResponse]:
        with self._transaction() as session:
            stmt = select(MonitoringClusterStackOperationRow).where(
                MonitoringClusterStackOperationRow.status.in_(("queued", "running"))
            )
            try:
                rows = list(session.scalars(stmt))
            except (OperationalError, ProgrammingError) as exc:
                if _is_missing_table_error(exc):
                    return []
                raise
            finished_at = datetime.now(UTC)
            for row in rows:
                dto = row.to_dto()
                dto.status = "failed"
                dto.finished_at = finished_at
                from llm_d_bench.monitoring.cluster_stack.models import OperationError

                dto.error = OperationError(
                    code="SERVICE_RESTARTED",
                    message=_RECOVERY_MESSAGE,
                    retryable=True,
                )
                row.sync_from_dto(dto, cluster_id=row.cluster_id)
            session.flush()
            return [row.to_dto() for row in rows]


def _is_missing_table_error(exc: OperationalError | ProgrammingError) -> bool:
    message = str(exc).lower()
    return "does not exist" in message or "no such table" in message
