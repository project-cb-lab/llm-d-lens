"""DAO for ``sessions`` -- see docs/design/auth-rbac-design.md sections 4.2.8/5.3.

Includes the TTL cleanup primitives the background sweep and login path use:
``sweep`` deletes expired/revoked rows in bounded batches, and
``trim_user_sessions`` enforces the per-user active-session cap.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from llm_d_bench.auth.records import SessionRecord, utcnow
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.session import SessionRow


def _to_record(row: SessionRow) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        created_at=row.created_at,
        expires_at=row.expires_at,
        idle_expires_at=row.idle_expires_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        auth_source=row.auth_source,
        ip=row.ip,
        user_agent=row.user_agent,
    )


class SessionDao(BaseDao):
    def create(self, record: SessionRecord) -> SessionRecord:
        if record.expires_at is None or record.idle_expires_at is None:
            raise ValueError("session requires expires_at and idle_expires_at")
        with self._transaction() as session:
            row = SessionRow(
                id=record.id,
                user_id=record.user_id,
                token_hash=record.token_hash,
                created_at=record.created_at,
                expires_at=record.expires_at,
                idle_expires_at=record.idle_expires_at,
                last_seen_at=record.last_seen_at or record.created_at,
                revoked_at=record.revoked_at,
                auth_source=record.auth_source,
                ip=record.ip,
                user_agent=record.user_agent,
            )
            session.add(row)
            session.flush()
            return _to_record(row)

    def get(self, session_id: str) -> SessionRecord | None:
        with self._read_only() as session:
            row = session.get(SessionRow, session_id)
            return _to_record(row) if row else None

    def get_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        with self._read_only() as session:
            stmt = select(SessionRow).where(SessionRow.token_hash == token_hash)
            row = session.scalars(stmt).first()
            return _to_record(row) if row else None

    def list_all(self, *, include_revoked: bool = False) -> list[SessionRecord]:
        with self._read_only() as session:
            stmt = select(SessionRow)
            if not include_revoked:
                stmt = stmt.where(SessionRow.revoked_at.is_(None))
            stmt = stmt.order_by(SessionRow.created_at.desc(), SessionRow.id)
            return [_to_record(row) for row in session.scalars(stmt)]

    def list_for_user(self, user_id: str, *, include_revoked: bool = False) -> list[SessionRecord]:
        with self._read_only() as session:
            stmt = select(SessionRow).where(SessionRow.user_id == user_id)
            if not include_revoked:
                stmt = stmt.where(SessionRow.revoked_at.is_(None))
            stmt = stmt.order_by(SessionRow.created_at.desc(), SessionRow.id)
            return [_to_record(row) for row in session.scalars(stmt)]

    def touch(self, session_id: str, *, idle_expires_at: datetime, last_seen_at: datetime) -> None:
        with self._transaction() as session:
            row = session.get(SessionRow, session_id)
            if row is not None and row.revoked_at is None:
                row.idle_expires_at = idle_expires_at
                row.last_seen_at = last_seen_at

    def revoke(self, session_id: str, *, when: datetime | None = None) -> None:
        with self._transaction() as session:
            row = session.get(SessionRow, session_id)
            if row is not None and row.revoked_at is None:
                row.revoked_at = when or utcnow()

    def revoke_user_sessions(
        self, user_id: str, *, when: datetime | None = None, keep_session_id: str | None = None
    ) -> int:
        """Revoke a user's sessions; optionally keep the current one (e.g. change-password).

        Returns the number of sessions actually revoked.
        """
        moment = when or utcnow()
        revoked = 0
        with self._transaction() as session:
            stmt = select(SessionRow).where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
            if keep_session_id:
                stmt = stmt.where(SessionRow.id != keep_session_id)
            for row in session.scalars(stmt):
                row.revoked_at = moment
                revoked += 1
        return revoked

    def count_active_for_user(self, user_id: str, *, now: datetime | None = None) -> int:
        moment = now or utcnow()
        with self._read_only() as session:
            stmt = (
                select(func.count())
                .select_from(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.revoked_at.is_(None),
                    SessionRow.expires_at > moment,
                    SessionRow.idle_expires_at > moment,
                )
            )
            return int(session.execute(stmt).scalar_one())

    def trim_user_sessions(self, user_id: str, max_keep: int) -> None:
        """Delete the oldest active sessions beyond ``max_keep`` for one user."""
        if max_keep < 0:
            raise ValueError("max_keep must be non-negative")
        with self._transaction() as session:
            stmt = (
                select(SessionRow)
                .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
                .order_by(SessionRow.created_at.desc(), SessionRow.id.desc())
            )
            for row in list(session.scalars(stmt))[max_keep:]:
                session.delete(row)

    def sweep(
        self,
        *,
        now: datetime | None = None,
        revoked_retention_seconds: int = 86400,
        batch_size: int = 1000,
    ) -> int:
        """Delete expired and long-revoked sessions in bounded batches.

        Returns the number of rows deleted. Safe to run concurrently: the
        ``DELETE ... WHERE`` predicate is idempotent.
        """
        from datetime import timedelta

        moment = now or utcnow()
        revoked_cutoff = moment - timedelta(seconds=revoked_retention_seconds)
        with self._transaction() as session:
            expired = session.scalars(
                select(SessionRow.id)
                .where(SessionRow.expires_at <= moment, SessionRow.revoked_at.is_(None))
                .limit(batch_size)
            ).all()
            revoked = session.scalars(
                select(SessionRow.id)
                .where(SessionRow.revoked_at.is_not(None), SessionRow.revoked_at <= revoked_cutoff)
                .limit(batch_size)
            ).all()
            ids = [*expired, *revoked]
            if ids:
                session.execute(sa_delete(SessionRow).where(SessionRow.id.in_(ids)))
            return len(ids)
