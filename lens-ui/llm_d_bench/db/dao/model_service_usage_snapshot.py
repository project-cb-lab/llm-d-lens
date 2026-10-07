"""DAO for the per-cluster EPP usage snapshots.

Stores the last cumulative EPP token counters so the usage sync can diff
successive scrapes into ledger deltas. See
``llm_d_bench/model_service/usage_sync.py``.
"""

from __future__ import annotations

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.model_service_usage_snapshot import ModelServiceUsageSnapshotRow
from llm_d_bench.model_service.contracts import EppUsageSnapshot


class ModelServiceUsageSnapshotDao(BaseDao):
    def get(self, cluster_id: str) -> EppUsageSnapshot | None:
        with self._read_only() as session:
            row = session.get(ModelServiceUsageSnapshotRow, cluster_id)
            return row.to_dto() if row else None

    def put(self, snapshot: EppUsageSnapshot) -> EppUsageSnapshot:
        with self._transaction() as session:
            row = session.get(ModelServiceUsageSnapshotRow, snapshot.cluster_id)
            if row is None:
                row = ModelServiceUsageSnapshotRow.from_dto(snapshot)
                session.add(row)
            else:
                row.sync_from_dto(snapshot)
            session.flush()
            return row.to_dto()
