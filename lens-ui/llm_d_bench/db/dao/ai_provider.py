"""DAO backing ``llm_d_bench.ai_providers.store`` -- see design doc section 5.4.4."""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.ai_providers.contracts import AIProvider
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.ai_provider import AIProviderRow


class AIProviderDaoError(Exception):
    """Raised for AI-provider-repository failures that aren't already a domain error."""


class AIProviderDao(BaseDao):
    def create(self, provider: AIProvider) -> AIProvider:
        with self._transaction() as session:
            if session.get(AIProviderRow, provider.id) is not None:
                raise AIProviderDaoError(f"AI provider already exists: {provider.id}")
            row = AIProviderRow.from_dto(provider)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, provider_id: str) -> AIProvider | None:
        with self._read_only() as session:
            row = session.get(AIProviderRow, provider_id)
            return row.to_dto() if row else None

    def list(self) -> list[AIProvider]:
        with self._read_only() as session:
            stmt = select(AIProviderRow).order_by(AIProviderRow.created_at, AIProviderRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, provider: AIProvider) -> AIProvider:
        """Full-record upsert-if-exists: replace every mapped column from ``provider``."""
        with self._transaction() as session:
            row = session.get(AIProviderRow, provider.id)
            if row is None:
                raise AIProviderDaoError(f"AI provider not found: {provider.id}")
            row.sync_from_dto(provider)
            session.flush()
            return row.to_dto()

    def delete(self, provider_id: str) -> None:
        with self._transaction() as session:
            row = session.get(AIProviderRow, provider_id)
            if row is not None:
                session.delete(row)
