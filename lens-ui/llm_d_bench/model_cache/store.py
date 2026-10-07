"""Durable storage for Model Cache entry records.

Backed by ``ModelCacheEntryDao`` (SQLAlchemy) -- see design doc
section 5.4.3. ``base_dir`` is accepted but ignored: it exists only so the
many existing call sites that construct ``ModelCacheStore(tmp_path)`` (to
get an isolated store per test) keep working unchanged; isolation is now
provided by the per-test database engine (see the repo root
``conftest.py``) instead of a per-instance directory.
"""

from __future__ import annotations

from pathlib import Path

from llm_d_bench.db.dao.model_cache_entry import ModelCacheEntryDao, ModelCacheEntryDaoError
from llm_d_bench.model_cache.contracts import ModelCacheEntry


class ModelCacheStoreError(ModelCacheEntryDaoError):
    """Base error for the Model Cache entry store (alias of the repository error)."""


class ModelCacheStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        del base_dir  # unused -- see module docstring
        self._dao = ModelCacheEntryDao()

    def create(self, entry: ModelCacheEntry) -> ModelCacheEntry:
        try:
            return self._dao.create(entry)
        except ModelCacheEntryDaoError as error:
            raise ModelCacheStoreError(str(error)) from error

    def get(self, entry_id: str) -> ModelCacheEntry | None:
        return self._dao.get(entry_id)

    def list(self) -> list[ModelCacheEntry]:
        return self._dao.list()

    def find_by_volume_and_source(self, storage_volume_id: str, source_display: str) -> ModelCacheEntry | None:
        """Return an existing entry for the same (volume, source) pair, if any.

        Used to make download requests idempotent (see design doc section 11).
        """
        return self._dao.find_by_volume_and_source(storage_volume_id, source_display)

    def list_for_volume(self, storage_volume_id: str) -> list[ModelCacheEntry]:
        return self._dao.list_for_volume(storage_volume_id)

    def save(self, entry: ModelCacheEntry) -> ModelCacheEntry:
        try:
            return self._dao.save(entry)
        except ModelCacheEntryDaoError as error:
            raise ModelCacheStoreError(str(error)) from error

    def delete(self, entry_id: str) -> None:
        self._dao.delete(entry_id)


_default_store: ModelCacheStore | None = None


def default_store() -> ModelCacheStore:
    global _default_store
    if _default_store is None:
        _default_store = ModelCacheStore()
    return _default_store
