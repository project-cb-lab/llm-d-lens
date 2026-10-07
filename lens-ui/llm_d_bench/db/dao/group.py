"""DAOs for ``groups``, ``user_groups`` and ``group_role_bindings``.

See docs/design/auth-rbac-design.md sections 4.2.2, 4.2.3 and 4.2.7.
"""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from llm_d_bench.auth.records import GroupRecord, GroupRoleBindingRecord, UserGroupRecord, utcnow
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.group import GroupRoleBindingRow, GroupRow, UserGroupRow


class GroupDaoError(Exception):
    """Raised for group-repository failures that aren't already a domain error."""


def _group_to_record(row: GroupRow) -> GroupRecord:
    return GroupRecord(
        id=row.id,
        name=row.name,
        description=row.description,
        source=row.source,
        provider_id=row.provider_id,
        external_id=row.external_id,
        authz_version=row.authz_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _binding_to_record(row: GroupRoleBindingRow) -> GroupRoleBindingRecord:
    return GroupRoleBindingRecord(
        id=row.id,
        group_id=row.group_id,
        role_id=row.role_id,
        scope_type=row.scope_type,
        scope_cluster_id=row.scope_cluster_id,
        scope_resource_type=row.scope_resource_type,
        scope_resource_id=row.scope_resource_id,
        granted_by_user_id=row.granted_by_user_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


class GroupDao(BaseDao):
    def create(self, record: GroupRecord) -> GroupRecord:
        with self._transaction() as session:
            row = GroupRow(
                id=record.id,
                name=record.name,
                description=record.description,
                source=record.source,
                provider_id=record.provider_id,
                external_id=record.external_id,
                authz_version=record.authz_version,
                created_at=record.created_at,
            )
            session.add(row)
            session.flush()
            return _group_to_record(row)

    def get(self, group_id: str) -> GroupRecord | None:
        with self._read_only() as session:
            row = session.get(GroupRow, group_id)
            return _group_to_record(row) if row else None

    def get_by_name(self, name: str) -> GroupRecord | None:
        with self._read_only() as session:
            stmt = select(GroupRow).where(func.lower(GroupRow.name) == name.strip().lower())
            row = session.scalars(stmt).first()
            return _group_to_record(row) if row else None

    def list(self) -> list[GroupRecord]:
        with self._read_only() as session:
            stmt = select(GroupRow).order_by(func.lower(GroupRow.name), GroupRow.id)
            return [_group_to_record(row) for row in session.scalars(stmt)]

    def get_by_external(self, provider_id: str, external_id: str) -> GroupRecord | None:
        with self._read_only() as session:
            stmt = select(GroupRow).where(
                GroupRow.provider_id == provider_id, GroupRow.external_id == external_id
            )
            row = session.scalars(stmt).first()
            return _group_to_record(row) if row else None

    def list_for_provider(self, provider_id: str) -> list[GroupRecord]:
        with self._read_only() as session:
            stmt = (
                select(GroupRow)
                .where(GroupRow.provider_id == provider_id)
                .order_by(func.lower(GroupRow.name), GroupRow.id)
            )
            return [_group_to_record(row) for row in session.scalars(stmt)]

    def save(self, record: GroupRecord) -> GroupRecord:
        with self._transaction() as session:
            row = session.get(GroupRow, record.id)
            if row is None:
                raise GroupDaoError(f"group not found: {record.id}")
            row.name = record.name
            row.description = record.description
            row.source = record.source
            row.provider_id = record.provider_id
            row.external_id = record.external_id
            row.authz_version = record.authz_version
            row.updated_at = utcnow()
            session.flush()
            return _group_to_record(row)

    def delete(self, group_id: str) -> None:
        with self._transaction() as session:
            row = session.get(GroupRow, group_id)
            if row is not None:
                session.delete(row)

    def bump_authz_version(self, group_id: str) -> None:
        """Invalidate members' cached authorization when membership changes (section 7.12)."""
        with self._transaction() as session:
            row = session.get(GroupRow, group_id)
            if row is not None:
                row.authz_version = (row.authz_version or 1) + 1
                row.updated_at = utcnow()


class UserGroupDao(BaseDao):
    def add(self, record: UserGroupRecord) -> UserGroupRecord:
        with self._transaction() as session:
            row = session.get(UserGroupRow, (record.user_id, record.group_id))
            if row is None:
                row = UserGroupRow(
                    user_id=record.user_id,
                    group_id=record.group_id,
                    source=record.source,
                    created_at=record.created_at,
                )
                session.add(row)
            else:
                row.source = record.source
            session.flush()
            return UserGroupRecord(
                user_id=row.user_id, group_id=row.group_id, source=row.source, created_at=row.created_at
            )

    def list_for_user(self, user_id: str) -> list[UserGroupRecord]:
        with self._read_only() as session:
            stmt = select(UserGroupRow).where(UserGroupRow.user_id == user_id)
            return [
                UserGroupRecord(
                    user_id=row.user_id, group_id=row.group_id, source=row.source, created_at=row.created_at
                )
                for row in session.scalars(stmt)
            ]

    def list_all(self) -> list[UserGroupRecord]:
        with self._read_only() as session:
            stmt = select(UserGroupRow)
            return [
                UserGroupRecord(
                    user_id=row.user_id, group_id=row.group_id, source=row.source, created_at=row.created_at
                )
                for row in session.scalars(stmt)
            ]

    def list_for_group(self, group_id: str) -> list[UserGroupRecord]:
        with self._read_only() as session:
            stmt = select(UserGroupRow).where(UserGroupRow.group_id == group_id)
            return [
                UserGroupRecord(
                    user_id=row.user_id, group_id=row.group_id, source=row.source, created_at=row.created_at
                )
                for row in session.scalars(stmt)
            ]

    def remove(self, user_id: str, group_id: str) -> None:
        with self._transaction() as session:
            row = session.get(UserGroupRow, (user_id, group_id))
            if row is not None:
                session.delete(row)

    def set_members(self, group_id: str, user_ids: set[str], *, source: str = "manual") -> None:
        """Replace the group's memberships, preserving rows whose source differs."""
        with self._transaction() as session:
            if source == "manual":
                session.execute(
                    sa_delete(UserGroupRow).where(UserGroupRow.group_id == group_id, UserGroupRow.source == "manual")
                )
            for user_id in sorted(user_ids):
                if session.get(UserGroupRow, (user_id, group_id)) is None:
                    session.add(UserGroupRow(user_id=user_id, group_id=group_id, source=source))


class GroupRoleBindingDao(BaseDao):
    def create(self, record: GroupRoleBindingRecord) -> GroupRoleBindingRecord:
        with self._transaction() as session:
            row = GroupRoleBindingRow(**_binding_columns(record))
            session.add(row)
            session.flush()
            return _binding_to_record(row)

    def list_for_group(self, group_id: str) -> list[GroupRoleBindingRecord]:
        with self._read_only() as session:
            stmt = (
                select(GroupRoleBindingRow)
                .where(GroupRoleBindingRow.group_id == group_id)
                .order_by(GroupRoleBindingRow.created_at, GroupRoleBindingRow.id)
            )
            return [_binding_to_record(row) for row in session.scalars(stmt)]

    def list_all(self) -> list[GroupRoleBindingRecord]:
        with self._read_only() as session:
            stmt = select(GroupRoleBindingRow).order_by(GroupRoleBindingRow.created_at, GroupRoleBindingRow.id)
            return [_binding_to_record(row) for row in session.scalars(stmt)]

    def get(self, binding_id: str) -> GroupRoleBindingRecord | None:
        with self._read_only() as session:
            row = session.get(GroupRoleBindingRow, binding_id)
            return _binding_to_record(row) if row else None

    def delete(self, binding_id: str) -> None:
        with self._transaction() as session:
            row = session.get(GroupRoleBindingRow, binding_id)
            if row is not None:
                session.delete(row)


def _binding_columns(record: GroupRoleBindingRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "group_id": record.group_id,
        "role_id": record.role_id,
        "scope_type": record.scope_type,
        "scope_cluster_id": record.scope_cluster_id,
        "scope_resource_type": record.scope_resource_type,
        "scope_resource_id": record.scope_resource_id,
        "granted_by_user_id": record.granted_by_user_id,
        "expires_at": record.expires_at,
        "created_at": record.created_at,
    }
