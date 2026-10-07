"""DAO for model-service members (published deployments).

Design reference: docs/design/model-service-v2-design.md section 4.3.
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.model_service_member import ModelServiceMemberRow
from llm_d_bench.model_service.contracts import ModelServiceMember


class ModelServiceMemberDaoError(Exception):
    """Raised for member-repository failures that aren't already a domain error."""


class ModelServiceMemberDao(BaseDao):
    def create(self, member: ModelServiceMember) -> ModelServiceMember:
        with self._transaction() as session:
            if session.get(ModelServiceMemberRow, member.id) is not None:
                raise ModelServiceMemberDaoError(f"member already exists: {member.id}")
            duplicate = session.scalars(
                select(ModelServiceMemberRow).where(
                    ModelServiceMemberRow.group_id == member.group_id,
                    ModelServiceMemberRow.execution_id == member.execution_id,
                )
            ).first()
            if duplicate is not None:
                raise ModelServiceMemberDaoError(f"execution already published to this group: {member.execution_id}")
            row = ModelServiceMemberRow.from_dto(member)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, member_id: str) -> ModelServiceMember | None:
        with self._read_only() as session:
            row = session.get(ModelServiceMemberRow, member_id)
            return row.to_dto() if row else None

    def list_by_group(self, group_id: str, *, status: str | None = None) -> list[ModelServiceMember]:
        with self._read_only() as session:
            stmt = select(ModelServiceMemberRow).where(ModelServiceMemberRow.group_id == group_id)
            if status is not None:
                stmt = stmt.where(ModelServiceMemberRow.status == status)
            stmt = stmt.order_by(ModelServiceMemberRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def list_all(self) -> list[ModelServiceMember]:
        with self._read_only() as session:
            stmt = select(ModelServiceMemberRow).order_by(ModelServiceMemberRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def list_by_cluster(self, cluster_id: str, *, status: str | None = None) -> list[ModelServiceMember]:
        with self._read_only() as session:
            stmt = select(ModelServiceMemberRow).where(ModelServiceMemberRow.cluster_id == cluster_id)
            if status is not None:
                stmt = stmt.where(ModelServiceMemberRow.status == status)
            stmt = stmt.order_by(ModelServiceMemberRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, member: ModelServiceMember) -> ModelServiceMember:
        with self._transaction() as session:
            row = session.get(ModelServiceMemberRow, member.id)
            if row is None:
                raise ModelServiceMemberDaoError(f"member not found: {member.id}")
            row.sync_from_dto(member)
            session.flush()
            return row.to_dto()

    def delete(self, member_id: str) -> None:
        with self._transaction() as session:
            row = session.get(ModelServiceMemberRow, member_id)
            if row is not None:
                session.delete(row)
