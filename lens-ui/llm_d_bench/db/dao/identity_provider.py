"""DAOs for ``identity_providers`` and ``identity_group_mappings``.

See docs/design/auth-rbac-design.md sections 4.2.9, 4.2.10 and 20.
"""

from __future__ import annotations

from sqlalchemy import func, select

from llm_d_bench.auth.records import IdentityGroupMappingRecord, IdentityProviderRecord, utcnow
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.identity_provider import IdentityGroupMappingRow, IdentityProviderRow


class IdentityProviderDaoError(Exception):
    """Raised for identity-provider-repository failures not already a domain error."""


def _to_record(row: IdentityProviderRow) -> IdentityProviderRecord:
    return IdentityProviderRecord(
        id=row.id,
        type=row.type,
        name=row.name,
        enabled=row.enabled,
        is_default=row.is_default,
        config=dict(row.config or {}),
        secret_encrypted=row.secret_encrypted,
        sync_mode=row.sync_mode,
        last_sync_at=row.last_sync_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class IdentityProviderDao(BaseDao):
    def create(self, record: IdentityProviderRecord) -> IdentityProviderRecord:
        with self._transaction() as session:
            row = IdentityProviderRow(
                id=record.id,
                type=record.type,
                name=record.name,
                enabled=record.enabled,
                is_default=record.is_default,
                config=dict(record.config),
                secret_encrypted=record.secret_encrypted,
                sync_mode=record.sync_mode,
                last_sync_at=record.last_sync_at,
                created_at=record.created_at,
            )
            session.add(row)
            session.flush()
            return _to_record(row)

    def get(self, provider_id: str) -> IdentityProviderRecord | None:
        with self._read_only() as session:
            row = session.get(IdentityProviderRow, provider_id)
            return _to_record(row) if row else None

    def get_by_name(self, name: str) -> IdentityProviderRecord | None:
        with self._read_only() as session:
            stmt = select(IdentityProviderRow).where(func.lower(IdentityProviderRow.name) == name.strip().lower())
            row = session.scalars(stmt).first()
            return _to_record(row) if row else None

    def list(self) -> list[IdentityProviderRecord]:
        with self._read_only() as session:
            stmt = select(IdentityProviderRow).order_by(func.lower(IdentityProviderRow.name), IdentityProviderRow.id)
            return [_to_record(row) for row in session.scalars(stmt)]

    def list_enabled(self) -> list[IdentityProviderRecord]:
        with self._read_only() as session:
            stmt = (
                select(IdentityProviderRow)
                .where(IdentityProviderRow.enabled.is_(True))
                .order_by(IdentityProviderRow.is_default.desc(), func.lower(IdentityProviderRow.name))
            )
            return [_to_record(row) for row in session.scalars(stmt)]

    def save(self, record: IdentityProviderRecord) -> IdentityProviderRecord:
        with self._transaction() as session:
            row = session.get(IdentityProviderRow, record.id)
            if row is None:
                raise IdentityProviderDaoError(f"identity provider not found: {record.id}")
            row.type = record.type
            row.name = record.name
            row.enabled = record.enabled
            row.is_default = record.is_default
            row.config = dict(record.config)
            row.secret_encrypted = record.secret_encrypted
            row.sync_mode = record.sync_mode
            row.last_sync_at = record.last_sync_at
            row.updated_at = utcnow()
            session.flush()
            return _to_record(row)

    def delete(self, provider_id: str) -> None:
        with self._transaction() as session:
            row = session.get(IdentityProviderRow, provider_id)
            if row is not None:
                session.delete(row)


class IdentityGroupMappingDao(BaseDao):
    def create(self, record: IdentityGroupMappingRecord) -> IdentityGroupMappingRecord:
        with self._transaction() as session:
            row = IdentityGroupMappingRow(
                id=record.id,
                provider_id=record.provider_id,
                external_group=record.external_group,
                group_id=record.group_id,
                role_id=record.role_id,
                created_at=record.created_at,
            )
            session.add(row)
            session.flush()
            return _mapping_to_record(row)

    def list_for_provider(self, provider_id: str) -> list[IdentityGroupMappingRecord]:
        with self._read_only() as session:
            stmt = (
                select(IdentityGroupMappingRow)
                .where(IdentityGroupMappingRow.provider_id == provider_id)
                .order_by(IdentityGroupMappingRow.external_group, IdentityGroupMappingRow.id)
            )
            return [_mapping_to_record(row) for row in session.scalars(stmt)]

    def list_all(self) -> list[IdentityGroupMappingRecord]:
        with self._read_only() as session:
            stmt = select(IdentityGroupMappingRow).order_by(
                IdentityGroupMappingRow.external_group, IdentityGroupMappingRow.id
            )
            return [_mapping_to_record(row) for row in session.scalars(stmt)]

    def delete(self, mapping_id: str) -> None:
        with self._transaction() as session:
            row = session.get(IdentityGroupMappingRow, mapping_id)
            if row is not None:
                session.delete(row)


def _mapping_to_record(row: IdentityGroupMappingRow) -> IdentityGroupMappingRecord:
    return IdentityGroupMappingRecord(
        id=row.id,
        provider_id=row.provider_id,
        external_group=row.external_group,
        group_id=row.group_id,
        role_id=row.role_id,
        created_at=row.created_at,
    )
