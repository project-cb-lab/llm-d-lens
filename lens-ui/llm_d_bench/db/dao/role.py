"""DAOs for ``roles``, ``role_permissions`` and ``user_role_bindings``.

See docs/design/auth-rbac-design.md sections 4.2.4-4.2.6.
"""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from llm_d_bench.auth.records import RolePermissionRecord, RoleRecord, UserRoleBindingRecord
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.role import RolePermissionRow, RoleRow, UserRoleBindingRow


class RoleDaoError(Exception):
    """Raised for role-repository failures that aren't already a domain error."""


def _role_to_record(row: RoleRow) -> RoleRecord:
    return RoleRecord(
        id=row.id,
        name=row.name,
        description=row.description,
        is_builtin=row.is_builtin,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _binding_to_record(row: UserRoleBindingRow) -> UserRoleBindingRecord:
    return UserRoleBindingRecord(
        id=row.id,
        user_id=row.user_id,
        role_id=row.role_id,
        scope_type=row.scope_type,
        scope_cluster_id=row.scope_cluster_id,
        scope_resource_type=row.scope_resource_type,
        scope_resource_id=row.scope_resource_id,
        granted_by_user_id=row.granted_by_user_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


class RoleDao(BaseDao):
    def create(self, record: RoleRecord) -> RoleRecord:
        with self._transaction() as session:
            row = RoleRow(
                id=record.id,
                name=record.name,
                description=record.description,
                is_builtin=record.is_builtin,
                created_at=record.created_at,
            )
            session.add(row)
            session.flush()
            return _role_to_record(row)

    def get(self, role_id: str) -> RoleRecord | None:
        with self._read_only() as session:
            row = session.get(RoleRow, role_id)
            return _role_to_record(row) if row else None

    def get_by_name(self, name: str) -> RoleRecord | None:
        with self._read_only() as session:
            stmt = select(RoleRow).where(func.lower(RoleRow.name) == name.strip().lower())
            row = session.scalars(stmt).first()
            return _role_to_record(row) if row else None

    def list(self) -> list[RoleRecord]:
        with self._read_only() as session:
            stmt = select(RoleRow).order_by(func.lower(RoleRow.name), RoleRow.id)
            return [_role_to_record(row) for row in session.scalars(stmt)]

    def save(self, record: RoleRecord) -> RoleRecord:
        with self._transaction() as session:
            row = session.get(RoleRow, record.id)
            if row is None:
                raise RoleDaoError(f"role not found: {record.id}")
            row.name = record.name
            row.description = record.description
            session.flush()
            return _role_to_record(row)

    def delete(self, role_id: str) -> None:
        with self._transaction() as session:
            row = session.get(RoleRow, role_id)
            if row is not None:
                session.delete(row)


class RolePermissionDao(BaseDao):
    def set_for_role(self, role_id: str, permissions: set[str] | frozenset[str]) -> None:
        """Replace the full permission set of a role in one transaction."""
        with self._transaction() as session:
            session.execute(sa_delete(RolePermissionRow).where(RolePermissionRow.role_id == role_id))
            for permission in sorted(permissions):
                session.add(RolePermissionRow(role_id=role_id, permission=permission))

    def list_for_role(self, role_id: str) -> list[RolePermissionRecord]:
        with self._read_only() as session:
            stmt = select(RolePermissionRow).where(RolePermissionRow.role_id == role_id)
            return [
                RolePermissionRecord(role_id=row.role_id, permission=row.permission) for row in session.scalars(stmt)
            ]

    def permissions_for_roles(self, role_ids: set[str] | frozenset[str] | list[str]) -> dict[str, frozenset[str]]:
        """Return ``{role_id: {permission, ...}}`` for the requested roles."""
        ids = list(role_ids)
        result: dict[str, set[str]] = {role_id: set() for role_id in ids}
        if not ids:
            return {role_id: frozenset() for role_id in ids}
        with self._read_only() as session:
            stmt = select(RolePermissionRow).where(RolePermissionRow.role_id.in_(ids))
            for row in session.scalars(stmt):
                result.setdefault(row.role_id, set()).add(row.permission)
        return {role_id: frozenset(codes) for role_id, codes in result.items()}


class UserRoleBindingDao(BaseDao):
    def create(self, record: UserRoleBindingRecord) -> UserRoleBindingRecord:
        with self._transaction() as session:
            row = UserRoleBindingRow(**_binding_columns(record))
            session.add(row)
            session.flush()
            return _binding_to_record(row)

    def list_for_user(self, user_id: str) -> list[UserRoleBindingRecord]:
        with self._read_only() as session:
            stmt = (
                select(UserRoleBindingRow)
                .where(UserRoleBindingRow.user_id == user_id)
                .order_by(UserRoleBindingRow.created_at, UserRoleBindingRow.id)
            )
            return [_binding_to_record(row) for row in session.scalars(stmt)]

    def list_all(self) -> list[UserRoleBindingRecord]:
        with self._read_only() as session:
            stmt = select(UserRoleBindingRow).order_by(UserRoleBindingRow.created_at, UserRoleBindingRow.id)
            return [_binding_to_record(row) for row in session.scalars(stmt)]

    def get(self, binding_id: str) -> UserRoleBindingRecord | None:
        with self._read_only() as session:
            row = session.get(UserRoleBindingRow, binding_id)
            return _binding_to_record(row) if row else None

    def delete(self, binding_id: str) -> None:
        with self._transaction() as session:
            row = session.get(UserRoleBindingRow, binding_id)
            if row is not None:
                session.delete(row)


def _binding_columns(record: UserRoleBindingRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "role_id": record.role_id,
        "scope_type": record.scope_type,
        "scope_cluster_id": record.scope_cluster_id,
        "scope_resource_type": record.scope_resource_type,
        "scope_resource_id": record.scope_resource_id,
        "granted_by_user_id": record.granted_by_user_id,
        "expires_at": record.expires_at,
        "created_at": record.created_at,
    }
