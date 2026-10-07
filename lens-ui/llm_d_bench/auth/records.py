"""Persistence records for the auth domain.

These plain dataclasses are the DTO boundary between the DAO layer and auth
services (design section 4). They intentionally hold no ORM or transport
concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class UserRecord:
    username: str
    display_name: str = ""
    id: str = field(default_factory=new_id)
    email: str | None = None
    password_hash: str | None = None
    provider_id: str | None = None
    auth_source: str = "local"
    external_id: str | None = None
    status: str = "active"
    failed_login_count: int = 0
    locked_until: datetime | None = None
    must_change_password: bool = False
    password_changed_at: datetime | None = None
    last_login_at: datetime | None = None
    principal_version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class GroupRecord:
    name: str
    description: str = ""
    id: str = field(default_factory=new_id)
    source: str = "local"
    provider_id: str | None = None
    external_id: str | None = None
    authz_version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class UserGroupRecord:
    user_id: str
    group_id: str
    source: str = "manual"
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class RoleRecord:
    name: str
    description: str = ""
    id: str = field(default_factory=new_id)
    is_builtin: bool = False
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class RolePermissionRecord:
    role_id: str
    permission: str


@dataclass
class UserRoleBindingRecord:
    user_id: str
    role_id: str
    id: str = field(default_factory=new_id)
    scope_type: str = "global"
    scope_cluster_id: str | None = None
    scope_resource_type: str | None = None
    scope_resource_id: str | None = None
    granted_by_user_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class GroupRoleBindingRecord:
    group_id: str
    role_id: str
    id: str = field(default_factory=new_id)
    scope_type: str = "global"
    scope_cluster_id: str | None = None
    scope_resource_type: str | None = None
    scope_resource_id: str | None = None
    granted_by_user_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class SessionRecord:
    user_id: str
    token_hash: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    idle_expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    auth_source: str = "local"
    ip: str | None = None
    user_agent: str | None = None


@dataclass
class IdentityProviderRecord:
    type: str
    name: str
    id: str = field(default_factory=new_id)
    enabled: bool = False
    is_default: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    secret_encrypted: str | None = None
    sync_mode: str = "login"
    last_sync_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class IdentityGroupMappingRecord:
    provider_id: str
    external_group: str
    id: str = field(default_factory=new_id)
    group_id: str | None = None
    role_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class AuditLogRecord:
    event_type: str
    actor_username: str = ""
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    actor_user_id: str | None = None
    permission: str | None = None
    method: str | None = None
    path: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    cluster_id: str | None = None
    result: str = "success"
    detail: dict[str, Any] | None = None
    ip: str | None = None
