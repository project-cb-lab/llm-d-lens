"""``sessions`` table -- see docs/design/auth-rbac-design.md sections 4.2.8/5.3.

Server-side, revocable login sessions. Only the SHA-256 hash of the opaque
token is stored. ``expires_at`` is the absolute cap and ``idle_expires_at`` the
sliding idle cap; both are enforced at validation time regardless of cleanup.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, UTCDateTime


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    auth_source: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index("uq_sessions_token_hash", "token_hash", unique=True),
        Index("ix_sessions_sweep", "expires_at", "idle_expires_at"),
        Index("ix_sessions_user", "user_id", "revoked_at"),
    )
