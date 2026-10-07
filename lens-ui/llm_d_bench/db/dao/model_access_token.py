"""DAO backing the model-service token service.

Design reference: docs/design/model-service-v2-design.md section 4.4.
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.model_access_token import ModelAccessTokenRow
from llm_d_bench.model_service.contracts import ModelAccessToken


class ModelAccessTokenDaoError(Exception):
    """Raised for token-repository failures that aren't already a domain error."""


class ModelAccessTokenDao(BaseDao):
    def create(self, token: ModelAccessToken) -> ModelAccessToken:
        with self._transaction() as session:
            if session.get(ModelAccessTokenRow, token.id) is not None:
                raise ModelAccessTokenDaoError(f"token already exists: {token.id}")
            row = ModelAccessTokenRow.from_dto(token)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, token_id: str) -> ModelAccessToken | None:
        with self._read_only() as session:
            row = session.get(ModelAccessTokenRow, token_id)
            return row.to_dto() if row else None

    def get_by_hash(self, token_hash: str) -> ModelAccessToken | None:
        with self._read_only() as session:
            stmt = select(ModelAccessTokenRow).where(ModelAccessTokenRow.token_hash == token_hash)
            row = session.scalars(stmt).first()
            return row.to_dto() if row else None

    def list_for_user(self, user_id: str, *, status: str | None = None) -> list[ModelAccessToken]:
        with self._read_only() as session:
            stmt = select(ModelAccessTokenRow).where(ModelAccessTokenRow.user_id == user_id)
            if status is not None:
                stmt = stmt.where(ModelAccessTokenRow.status == status)
            stmt = stmt.order_by(ModelAccessTokenRow.created_at, ModelAccessTokenRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, token: ModelAccessToken) -> ModelAccessToken:
        with self._transaction() as session:
            row = session.get(ModelAccessTokenRow, token.id)
            if row is None:
                raise ModelAccessTokenDaoError(f"token not found: {token.id}")
            row.sync_from_dto(token)
            session.flush()
            return row.to_dto()

    def delete(self, token_id: str) -> None:
        with self._transaction() as session:
            row = session.get(ModelAccessTokenRow, token_id)
            if row is not None:
                session.delete(row)
