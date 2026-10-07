"""``audit_logs`` table -- see docs/design/auth-rbac-design.md section 4.2.11.

Append-only authentication and authorization trail. The actor id is kept only
as a snapshot-friendly reference (``SET NULL`` on user deletion) alongside the
username so history survives account removal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, JSONVariant, UTCDateTime


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_username: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    permission: Mapped[str | None] = mapped_column(String(100), nullable=True)
    method: Mapped[str | None] = mapped_column(String(8), nullable=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_logs_event_created", "event_type", "created_at"),
    )
