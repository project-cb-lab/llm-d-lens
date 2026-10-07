"""DAO for model-service routing groups.

Design reference: docs/design/model-service-v2-design.md section 4.2.
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.model_service_group import ModelServiceGroupRow
from llm_d_bench.model_service.contracts import ModelServiceGroup


class ModelServiceGroupDaoError(Exception):
    """Raised for group-repository failures that aren't already a domain error."""


class ModelServiceGroupDao(BaseDao):
    def create(self, group: ModelServiceGroup) -> ModelServiceGroup:
        with self._transaction() as session:
            if session.get(ModelServiceGroupRow, group.id) is not None:
                raise ModelServiceGroupDaoError(f"group already exists: {group.id}")
            # One model service per public name per cluster; a service is scoped
            # to its own cluster and only serves providers from that cluster.
            stmt = select(ModelServiceGroupRow).where(
                ModelServiceGroupRow.name == group.name,
                ModelServiceGroupRow.cluster_id == group.cluster_id,
            )
            existing = session.scalars(stmt).first()
            if existing is not None:
                raise ModelServiceGroupDaoError(
                    f"model service name already in use in cluster {group.cluster_id}: {group.name}"
                )
            row = ModelServiceGroupRow.from_dto(group)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, group_id: str) -> ModelServiceGroup | None:
        with self._read_only() as session:
            row = session.get(ModelServiceGroupRow, group_id)
            return row.to_dto() if row else None

    def get_by_name(self, name: str) -> ModelServiceGroup | None:
        with self._read_only() as session:
            stmt = select(ModelServiceGroupRow).where(ModelServiceGroupRow.name == name)
            row = session.scalars(stmt).first()
            return row.to_dto() if row else None

    def list(self) -> list[ModelServiceGroup]:
        with self._read_only() as session:
            stmt = select(ModelServiceGroupRow).order_by(ModelServiceGroupRow.created_at, ModelServiceGroupRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, group: ModelServiceGroup) -> ModelServiceGroup:
        with self._transaction() as session:
            row = session.get(ModelServiceGroupRow, group.id)
            if row is None:
                raise ModelServiceGroupDaoError(f"group not found: {group.id}")
            row.sync_from_dto(group)
            session.flush()
            return row.to_dto()

    def delete(self, group_id: str) -> None:
        with self._transaction() as session:
            row = session.get(ModelServiceGroupRow, group_id)
            if row is not None:
                session.delete(row)
