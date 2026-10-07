"""DAO backing ``llm_d_bench.model_cache.store`` -- see design doc section 5.4.3."""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.model_cache_entry import ModelCacheEntryRow
from llm_d_bench.model_cache.contracts import ModelCacheEntry


class ModelCacheEntryDaoError(Exception):
    """Raised for model-cache-entry-repository failures that aren't already a domain error."""


class ModelCacheEntryDao(BaseDao):
    def create(self, entry: ModelCacheEntry) -> ModelCacheEntry:
        with self._transaction() as session:
            if session.get(ModelCacheEntryRow, entry.id) is not None:
                raise ModelCacheEntryDaoError(f"model cache entry already exists: {entry.id}")
            row = ModelCacheEntryRow.from_dto(entry)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, entry_id: str) -> ModelCacheEntry | None:
        with self._read_only() as session:
            row = session.get(ModelCacheEntryRow, entry_id)
            return row.to_dto() if row else None

    def list(self) -> list[ModelCacheEntry]:
        with self._read_only() as session:
            stmt = select(ModelCacheEntryRow).order_by(ModelCacheEntryRow.created_at, ModelCacheEntryRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def find_by_volume_and_source(self, storage_volume_id: str, source_display: str) -> ModelCacheEntry | None:
        """Return an existing entry for the same (volume, source) pair, if any.

        Uses the ``uq_model_cache_volume_source`` index instead of the old
        file-store's full-table scan (see design doc section 5.4.3).
        """
        with self._read_only() as session:
            stmt = select(ModelCacheEntryRow).where(
                ModelCacheEntryRow.storage_volume_id == storage_volume_id,
                ModelCacheEntryRow.source_display == source_display,
            )
            row = session.scalars(stmt).one_or_none()
            return row.to_dto() if row else None

    def list_for_volume(self, storage_volume_id: str) -> list[ModelCacheEntry]:
        with self._read_only() as session:
            stmt = (
                select(ModelCacheEntryRow)
                .where(ModelCacheEntryRow.storage_volume_id == storage_volume_id)
                .order_by(ModelCacheEntryRow.created_at, ModelCacheEntryRow.id)
            )
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, entry: ModelCacheEntry) -> ModelCacheEntry:
        """Full-record upsert-if-exists: replace every mapped column from ``entry``."""
        with self._transaction() as session:
            row = session.get(ModelCacheEntryRow, entry.id)
            if row is None:
                raise ModelCacheEntryDaoError(f"model cache entry not found: {entry.id}")
            row.sync_from_dto(entry)
            session.flush()
            return row.to_dto()

    def delete(self, entry_id: str) -> None:
        with self._transaction() as session:
            row = session.get(ModelCacheEntryRow, entry_id)
            if row is not None:
                session.delete(row)
