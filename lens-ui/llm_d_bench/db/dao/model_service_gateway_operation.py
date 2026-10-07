"""DAO for model-service gateway operations.

Design reference: docs/design/model-service-gateway-deployment.md.
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.model_service_gateway_operation import ModelServiceGatewayOperationRow
from llm_d_bench.model_service.gateway_contracts import GatewayOperation


class GatewayOperationDaoError(Exception):
    """Raised for gateway-operation-repository failures that aren't already a domain error."""


class GatewayOperationDao(BaseDao):
    def create(self, operation: GatewayOperation) -> GatewayOperation:
        with self._transaction() as session:
            row = ModelServiceGatewayOperationRow.from_dto(operation)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, operation_id: str) -> GatewayOperation | None:
        with self._read_only() as session:
            row = session.get(ModelServiceGatewayOperationRow, operation_id)
            return row.to_dto() if row else None

    def list(self, *, limit: int = 50) -> list[GatewayOperation]:
        with self._read_only() as session:
            stmt = (
                select(ModelServiceGatewayOperationRow)
                .order_by(ModelServiceGatewayOperationRow.created_at.desc())
                .limit(limit)
            )
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, operation: GatewayOperation) -> GatewayOperation:
        with self._transaction() as session:
            row = session.get(ModelServiceGatewayOperationRow, operation.id)
            if row is None:
                raise GatewayOperationDaoError(f"operation not found: {operation.id}")
            row.sync_from_dto(operation)
            session.flush()
            return row.to_dto()

    def latest_for_kind(self, kind: str, *, cluster_id: str | None = None) -> GatewayOperation | None:
        with self._read_only() as session:
            stmt = select(ModelServiceGatewayOperationRow).where(ModelServiceGatewayOperationRow.kind == kind)
            if cluster_id is not None:
                stmt = stmt.where(ModelServiceGatewayOperationRow.cluster_id == cluster_id)
            stmt = stmt.order_by(ModelServiceGatewayOperationRow.created_at.desc()).limit(1)
            row = session.scalars(stmt).first()
            return row.to_dto() if row else None
