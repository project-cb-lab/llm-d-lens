"""DAO backing the auth user store -- see docs/design/auth-rbac-design.md section 4.2.1."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select

from llm_d_bench.auth.records import UserRecord, utcnow
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.user import UserRow


class UserDaoError(Exception):
    """Raised for user-repository failures that aren't already a domain error."""


def _to_record(row: UserRow) -> UserRecord:
    return UserRecord(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        email=row.email,
        password_hash=row.password_hash,
        provider_id=row.provider_id,
        auth_source=row.auth_source,
        external_id=row.external_id,
        status=row.status,
        failed_login_count=row.failed_login_count,
        locked_until=row.locked_until,
        must_change_password=row.must_change_password,
        password_changed_at=row.password_changed_at,
        last_login_at=row.last_login_at,
        principal_version=row.principal_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply(row: UserRow, record: UserRecord) -> None:
    row.username = record.username
    row.display_name = record.display_name
    row.email = record.email
    row.password_hash = record.password_hash
    row.provider_id = record.provider_id
    row.auth_source = record.auth_source
    row.external_id = record.external_id
    row.status = record.status
    row.failed_login_count = record.failed_login_count
    row.locked_until = record.locked_until
    row.must_change_password = record.must_change_password
    row.password_changed_at = record.password_changed_at
    row.last_login_at = record.last_login_at
    row.principal_version = record.principal_version
    row.updated_at = utcnow()


class UserDao(BaseDao):
    def create(self, record: UserRecord) -> UserRecord:
        with self._transaction() as session:
            row = UserRow(id=record.id)
            _apply(row, record)
            row.created_at = record.created_at
            session.add(row)
            session.flush()
            return _to_record(row)

    def get(self, user_id: str) -> UserRecord | None:
        with self._read_only() as session:
            row = session.get(UserRow, user_id)
            return _to_record(row) if row else None

    def get_by_username(self, username: str) -> UserRecord | None:
        with self._read_only() as session:
            stmt = select(UserRow).where(func.lower(UserRow.username) == username.strip().lower())
            row = session.scalars(stmt).first()
            return _to_record(row) if row else None

    def get_by_external(self, provider_id: str, external_id: str) -> UserRecord | None:
        with self._read_only() as session:
            stmt = select(UserRow).where(UserRow.provider_id == provider_id, UserRow.external_id == external_id)
            row = session.scalars(stmt).first()
            return _to_record(row) if row else None

    def list(self, *, query: str | None = None, status: str | None = None) -> list[UserRecord]:
        with self._read_only() as session:
            stmt = select(UserRow).order_by(func.lower(UserRow.username), UserRow.id)
            if status:
                stmt = stmt.where(UserRow.status == status)
            if query:
                needle = f"%{query.strip().lower()}%"
                stmt = stmt.where(
                    or_(
                        func.lower(UserRow.username).like(needle),
                        func.lower(UserRow.display_name).like(needle),
                        func.lower(func.coalesce(UserRow.email, "")).like(needle),
                    )
                )
            return [_to_record(row) for row in session.scalars(stmt)]

    def save(self, record: UserRecord) -> UserRecord:
        with self._transaction() as session:
            row = session.get(UserRow, record.id)
            if row is None:
                raise UserDaoError(f"user not found: {record.id}")
            _apply(row, record)
            session.flush()
            return _to_record(row)

    def delete(self, user_id: str) -> None:
        with self._transaction() as session:
            row = session.get(UserRow, user_id)
            if row is not None:
                session.delete(row)

    def bump_principal_version(self, user_id: str) -> None:
        """Invalidate cached authorization for a user (design section 7.12)."""
        with self._transaction() as session:
            row = session.get(UserRow, user_id)
            if row is not None:
                row.principal_version = (row.principal_version or 1) + 1
                row.updated_at = utcnow()

    def mark_login(self, user_id: str, *, when: datetime | None = None) -> None:
        with self._transaction() as session:
            row = session.get(UserRow, user_id)
            if row is not None:
                row.last_login_at = when or utcnow()
                row.failed_login_count = 0
                row.locked_until = None
                row.updated_at = utcnow()
