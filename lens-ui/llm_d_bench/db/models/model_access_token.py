"""``model_access_tokens`` table -- user machine credentials for the model gateway.

Only the SHA-256 hash of the plaintext token is stored. Backs
``llm_d_bench.model_service.contracts.ModelAccessToken``.
Design reference: docs/design/model-service-v2-design.md section 4.4.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, DeclarativeDtoMixin, UTCDateTime
from llm_d_bench.model_service.contracts import ModelAccessToken


class ModelAccessTokenRow(Base, DeclarativeDtoMixin):
    __tablename__ = "model_access_tokens"

    dto_type = ModelAccessToken
    column_map = {
        "id": "id",
        "user_id": "user_id",
        "name": "name",
        "token_hash": "token_hash",
        "token_hint": "token_hint",
        "status": "status",
        "expires_at": "expires_at",
        "last_used_at": "last_used_at",
        "last_used_ip": "last_used_ip",
        "created_at": "created_at",
        "revoked_at": "revoked_at",
    }

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version_id: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        Index("uq_model_access_tokens_hash", "token_hash", unique=True),
        Index("ix_model_access_tokens_user", "user_id", "status"),
    )
