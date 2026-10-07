"""Durable storage for External AI Provider records.

Backed by ``AIProviderDao`` (SQLAlchemy) -- see design doc section
5.4.4. ``base_dir`` is accepted but ignored: it exists only so the many
existing call sites that construct ``AIProviderStore(tmp_path)`` (to get an
isolated store per test) keep working unchanged; isolation is now provided
by the per-test database engine (see the repo root ``conftest.py``) instead
of a per-instance directory.
"""

from __future__ import annotations

from pathlib import Path

from llm_d_bench.ai_providers.contracts import AIProvider
from llm_d_bench.db.dao.ai_provider import AIProviderDao, AIProviderDaoError


class AIProviderStoreError(AIProviderDaoError):
    """Base error for the AI provider store (alias of the repository error)."""


class AIProviderStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        del base_dir  # unused -- see module docstring
        self._dao = AIProviderDao()

    def create(self, provider: AIProvider) -> AIProvider:
        try:
            return self._dao.create(provider)
        except AIProviderDaoError as error:
            raise AIProviderStoreError(str(error)) from error

    def get(self, provider_id: str) -> AIProvider | None:
        return self._dao.get(provider_id)

    def list(self) -> list[AIProvider]:
        return self._dao.list()

    def save(self, provider: AIProvider) -> AIProvider:
        try:
            return self._dao.save(provider)
        except AIProviderDaoError as error:
            raise AIProviderStoreError(str(error)) from error

    def delete(self, provider_id: str) -> None:
        self._dao.delete(provider_id)


_default_store: AIProviderStore | None = None


def default_store() -> AIProviderStore:
    global _default_store
    if _default_store is None:
        _default_store = AIProviderStore()
    return _default_store
