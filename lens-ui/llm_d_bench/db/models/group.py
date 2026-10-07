"""``groups``, ``user_groups`` and ``group_role_bindings`` tables.

See docs/design/auth-rbac-design.md sections 4.2.2, 4.2.3 and 4.2.7.
Groups are flat in v1 (no parent column, design section 7.14); nesting, if
ever added, is flattened at sync time rather than resolved at runtime.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, UTCDateTime


class GroupRow(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("identity_providers.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    authz_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        Index("uq_groups_name_lower", func.lower(name), unique=True),
        Index("ix_groups_source", "source"),
    )


class UserGroupRow(Base):
    __tablename__ = "user_groups"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_user_groups_group", "group_id"),)


class GroupRoleBindingRow(Base):
    __tablename__ = "group_role_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    scope_cluster_id: Mapped[str | None] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), nullable=True)
    scope_resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    granted_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global', 'cluster', 'resource')",
            name="ck_group_role_bindings_scope_type",
        ),
        Index("ix_group_role_bindings_group", "group_id"),
        Index("ix_group_role_bindings_cluster", "scope_cluster_id"),
    )
