"""``roles``, ``role_permissions`` and ``user_role_bindings`` tables.

See docs/design/auth-rbac-design.md sections 4.2.4, 4.2.5 and 4.2.6.
The permission catalog itself lives in ``llm_d_bench.auth.permissions``;
``role_permissions`` only stores which codes a role is bound to.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.db.base import Base, UTCDateTime


class RoleRow(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("uq_roles_name_lower", func.lower(name), unique=True),)


class RolePermissionRow(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(String(100), primary_key=True)


class UserRoleBindingRow(Base):
    __tablename__ = "user_role_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
            name="ck_user_role_bindings_scope_type",
        ),
        Index("ix_user_role_bindings_user", "user_id"),
        Index("ix_user_role_bindings_cluster", "scope_cluster_id"),
    )
