"""DAO for the per-request usage ledger.

Design reference: docs/design/model-service-v2-design.md section 4.5.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.usage_record import UsageRecordRow
from llm_d_bench.model_service.contracts import UsageRecord


class UsageRecordDaoError(Exception):
    """Raised for usage-repository failures that aren't already a domain error."""


class UsageRecordDao(BaseDao):
    def create(self, record: UsageRecord) -> UsageRecord:
        with self._transaction() as session:
            if session.get(UsageRecordRow, record.id) is not None:
                raise UsageRecordDaoError(f"usage record already exists: {record.id}")
            row = UsageRecordRow.from_dto(record)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get_by_request_id(self, request_id: str) -> UsageRecord | None:
        with self._read_only() as session:
            stmt = select(UsageRecordRow).where(UsageRecordRow.request_id == request_id)
            row = session.scalars(stmt).first()
            return row.to_dto() if row else None

    def list_for_user(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[UsageRecord]:
        with self._read_only() as session:
            stmt = select(UsageRecordRow).where(UsageRecordRow.user_id == user_id)
            if since is not None:
                stmt = stmt.where(UsageRecordRow.created_at >= since)
            if until is not None:
                stmt = stmt.where(UsageRecordRow.created_at <= until)
            stmt = stmt.order_by(UsageRecordRow.created_at.desc()).limit(limit).offset(offset)
            return [row.to_dto() for row in session.scalars(stmt)]

    def list_all(
        self,
        *,
        group_id: str | None = None,
        cluster_id: str | None = None,
        group_ids: Sequence[str] | None = None,
        cluster_ids: Sequence[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[UsageRecord]:
        with self._read_only() as session:
            stmt = select(UsageRecordRow)
            if group_id is not None:
                stmt = stmt.where(UsageRecordRow.group_id == group_id)
            if cluster_id is not None:
                stmt = stmt.where(UsageRecordRow.cluster_id == cluster_id)
            if group_ids is not None:
                stmt = stmt.where(UsageRecordRow.group_id.in_(group_ids))
            if cluster_ids is not None:
                stmt = stmt.where(UsageRecordRow.cluster_id.in_(cluster_ids))
            stmt = stmt.order_by(UsageRecordRow.created_at.desc()).limit(limit).offset(offset)
            return [row.to_dto() for row in session.scalars(stmt)]

    def list_range(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        user_id: str | None = None,
        group_id: str | None = None,
        cluster_id: str | None = None,
        user_ids: Sequence[str] | None = None,
        group_ids: Sequence[str] | None = None,
        cluster_ids: Sequence[str] | None = None,
        limit: int = 20000,
    ) -> list[UsageRecord]:
        """Filtered records (ascending) for aggregation over a time range.

        The singular ``*_id`` filters and the plural ``*_ids`` filters can be
        combined; both narrow the result (AND). ``*_ids`` supports multi-select
        filtering (e.g. several clusters at once).
        """
        with self._read_only() as session:
            stmt = select(UsageRecordRow)
            if since is not None:
                stmt = stmt.where(UsageRecordRow.created_at >= since)
            if until is not None:
                stmt = stmt.where(UsageRecordRow.created_at <= until)
            if user_id is not None:
                stmt = stmt.where(UsageRecordRow.user_id == user_id)
            if group_id is not None:
                stmt = stmt.where(UsageRecordRow.group_id == group_id)
            if cluster_id is not None:
                stmt = stmt.where(UsageRecordRow.cluster_id == cluster_id)
            if user_ids is not None:
                stmt = stmt.where(UsageRecordRow.user_id.in_(user_ids))
            if group_ids is not None:
                stmt = stmt.where(UsageRecordRow.group_id.in_(group_ids))
            if cluster_ids is not None:
                stmt = stmt.where(UsageRecordRow.cluster_id.in_(cluster_ids))
            stmt = stmt.order_by(UsageRecordRow.created_at.asc()).limit(limit)
            return [row.to_dto() for row in session.scalars(stmt)]

    def count_for_user(self, user_id: str) -> int:
        with self._read_only() as session:
            stmt = select(func.count()).select_from(UsageRecordRow).where(UsageRecordRow.user_id == user_id)
            return int(session.scalar(stmt) or 0)
