"""``identity_providers`` and ``identity_group_mappings`` tables.

See docs/design/auth-rbac-design.md sections 4.2.9, 4.2.10 and 20.
The provider ``type`` is the key into the pluggable ``IdentityProvider``
registry; protocol-specific fields live in the ``config`` JSON so new IdP
types do not change the schema. The encrypted secret is never returned by the
API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, JSONVariant, UTCDateTime


class IdentityProviderRow(Base):
    __tablename__ = "identity_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="login")
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        Index("uq_identity_providers_name_lower", func.lower(name), unique=True),
        Index("ix_identity_providers_type", "type"),
    )


class IdentityGroupMappingRow(Base):
    __tablename__ = "identity_group_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("identity_providers.id", ondelete="CASCADE"), nullable=False)
    external_group: Mapped[str] = mapped_column(String(512), nullable=False)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    role_id: Mapped[str | None] = mapped_column(ForeignKey("roles.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_identity_group_mappings", "provider_id", "external_group", unique=True),)
