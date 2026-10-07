"""Auth, user, group, role, identity-provider and audit HTTP endpoints.

See design sections 5.5 and 11. Enforcement is provided globally by
``auth.context.require_route_permission``; handlers own validation, scope
filtering and cookie/session mechanics.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from llm_d_bench.auth import master_key
from llm_d_bench.auth.bootstrap import clear_initial_admin_credentials
from llm_d_bench.auth.context import CSRF_COOKIE, SESSION_COOKIE
from llm_d_bench.auth.contracts import Principal
from llm_d_bench.auth.permissions import ALL_PERMISSIONS
from llm_d_bench.auth.policy import accessible_cluster_ids, authorize, principal_permissions
from llm_d_bench.auth.providers.registry import get_provider
from llm_d_bench.auth.records import (
    IdentityProviderRecord,
    RoleRecord,
    UserRecord,
)
from llm_d_bench.auth.service import default_service, is_protected_user
from llm_d_bench.auth.settings import get_settings
from llm_d_bench.core.exceptions import DomainValidationError, NotFoundError, UnauthenticatedError

router = APIRouter(prefix="/api/v1", tags=["auth"])


# --- serialization helpers ---------------------------------------------------
def _jsonify(value: Any) -> Any:
    """Recursively convert dataclasses/datetimes (including inside lists/dicts)
    to JSON-serializable values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _jsonify(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    return value


def _user_public(user: UserRecord) -> dict[str, Any]:
    data = _jsonify(user)
    data.pop("password_hash", None)
    data["has_password"] = user.password_hash is not None
    data["protected"] = is_protected_user(user)
    return data


def _binding_json(record: Any) -> dict[str, Any]:
    return _jsonify(record)


def _role_json(role: RoleRecord) -> dict[str, Any]:
    service = default_service()
    permissions = sorted(p.permission for p in service.role_permission_dao.list_for_role(role.id))
    data = _jsonify(role)
    data["permissions"] = permissions
    return data


def _provider_public(record: IdentityProviderRecord) -> dict[str, Any]:
    data = _jsonify(record)
    data.pop("secret_encrypted", None)
    data["hasSecret"] = record.secret_encrypted is not None
    return data


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise UnauthenticatedError("authentication required")
    return principal


def _set_session_cookies(response: Response, token: str, *, remember: bool = False) -> None:
    settings = get_settings()
    # Max-Age uses the absolute TTL, not the idle timeout: the idle cutoff is
    # enforced server-side on every request (session.idle_expires_at), and
    # slides forward while the user is active. Capping the cookie itself at
    # the (shorter) idle window would silently log an active user out the
    # instant that many seconds had passed since login, regardless of
    # activity -- exactly the sliding-renewal behavior this is meant to allow.
    max_age = settings.remember_session_ttl_seconds if remember else settings.session_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=max_age,
        httponly=False,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )


def _principal_view(principal: Principal) -> dict[str, Any]:
    clusters = accessible_cluster_ids(principal)
    return {
        "userId": principal.user_id,
        "username": principal.username,
        "permissions": sorted(principal_permissions(principal)),
        "reachableClusters": None if clusters is None else sorted(clusters),
        "mustChangePassword": principal.must_change_password,
        "authzVersion": principal.authz_version,
    }


# --- request models ----------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


class BootstrapRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(alias="oldPassword")
    new_password: str = Field(alias="newPassword")

    model_config = {"populate_by_name": True}


class UserCreateRequest(BaseModel):
    username: str
    password: str | None = None
    display_name: str = Field(default="", alias="displayName")
    email: str | None = None

    model_config = {"populate_by_name": True}


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, alias="displayName")
    email: str | None = None
    status: str | None = None

    model_config = {"populate_by_name": True}


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(alias="newPassword")

    model_config = {"populate_by_name": True}


class GroupCreateRequest(BaseModel):
    name: str
    description: str = ""


class GroupUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class MembersRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list, alias="userIds")

    model_config = {"populate_by_name": True}


class RoleCreateRequest(BaseModel):
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


class RolePermissionsRequest(BaseModel):
    permissions: list[str]


class BindingRequest(BaseModel):
    role_id: str = Field(alias="roleId")
    scope_type: str = Field(default="global", alias="scopeType")
    scope_cluster_id: str | None = Field(default=None, alias="scopeClusterId")
    scope_resource_type: str | None = Field(default=None, alias="scopeResourceType")
    scope_resource_id: str | None = Field(default=None, alias="scopeResourceId")

    model_config = {"populate_by_name": True}


class AccessRequest(BaseModel):
    subject_type: str = Field(alias="subjectType")  # user | group
    subject_id: str = Field(alias="subjectId")
    role_id: str = Field(alias="roleId")
    cluster_id: str | None = Field(default=None, alias="clusterId")

    model_config = {"populate_by_name": True}


class ProviderRequest(BaseModel):
    type: str
    name: str
    enabled: bool = False
    is_default: bool = Field(default=False, alias="isDefault")
    config: dict[str, Any] = Field(default_factory=dict)
    sync_mode: str = Field(default="login", alias="syncMode")
    secret: str | None = None

    model_config = {"populate_by_name": True}


class RotateSecretKeyRequest(BaseModel):
    new_key: str | None = Field(default=None, alias="newKey")

    model_config = {"populate_by_name": True}


# --- auth --------------------------------------------------------------------
@router.get("/auth/providers")
async def list_providers() -> list[dict[str, Any]]:
    service = default_service()
    providers = [{"type": "local", "name": "Lens", "enabled": True, "isDefault": True}]
    for record in service.identity_provider_dao.list_enabled():
        if record.type == "local":
            continue
        providers.append(
            {"type": record.type, "name": record.name, "enabled": record.enabled, "isDefault": record.is_default}
        )
    return providers


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    service = default_service()
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # No global auth-mode switch: ``login`` checks each username against
    # whichever source actually owns that account (local password, its own
    # identity provider, or — for a first-time directory login — every
    # enabled external provider).
    result = await service.login(body.username, body.password, ip=ip, user_agent=user_agent, remember=body.remember)
    _set_session_cookies(response, result.token, remember=body.remember)
    return {"user": _user_public(result.user), "principal": _principal_view(result.principal)}


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    service = default_service()
    token = getattr(request.state, "principal_token", None)
    if token:
        service.logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/auth/session")
async def current_session(request: Request) -> dict[str, Any]:
    return _principal_view(_principal(request))


@router.post("/auth/password", status_code=204)
async def change_password(body: PasswordChangeRequest, request: Request) -> None:
    principal = _principal(request)
    default_service().change_password(
        principal.user_id,
        body.old_password,
        body.new_password,
        keep_token=getattr(request.state, "principal_token", None),
    )
    # The first-run credential file is no longer needed once the password changes.
    clear_initial_admin_credentials()


@router.get("/auth/sessions")
async def my_sessions(request: Request) -> list[dict[str, Any]]:
    principal = _principal(request)
    return _jsonify(default_service().list_sessions(principal.user_id))


@router.get("/auth/permissions")
async def permission_catalog() -> list[dict[str, Any]]:
    return [
        {"code": p.code, "domain": p.domain, "description": p.description, "risk": p.risk}
        for p in sorted(ALL_PERMISSIONS.values(), key=lambda permission: permission.code)
    ]


@router.post("/auth/bootstrap")
async def bootstrap(body: BootstrapRequest) -> dict[str, Any]:
    user = default_service().bootstrap_admin(body.username, body.password)
    return _user_public(user)


# --- sessions ---------------------------------------------------------------
@router.get("/sessions")
async def list_sessions(request: Request, include_revoked: bool = False) -> list[dict[str, Any]]:
    principal = _principal(request)
    service = default_service()
    if authorize(principal, "session:session:revoke").allowed:
        usernames = {user.id: user.username for user in service.user_dao.list()}
        return [
            {**_jsonify(record), "username": usernames.get(record.user_id, record.user_id)}
            for record in service.session_dao.list_all(include_revoked=include_revoked)
        ]
    return _jsonify(service.list_sessions(principal.user_id))


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: str, request: Request) -> None:
    principal = _principal(request)
    default_service().revoke_session(
        session_id,
        actor=principal,
        allow_any=authorize(principal, "session:session:revoke").allowed,
    )


# --- users ------------------------------------------------------------------
@router.get("/users")
async def list_users(query: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    service = default_service()
    role_names = {role.id: role.name for role in service.role_dao.list()}
    group_names = {group.id: group.name for group in service.group_dao.list()}
    bindings_by_user: dict[str, list[Any]] = {}
    for binding in service.user_binding_dao.list_all():
        bindings_by_user.setdefault(binding.user_id, []).append(binding)
    groups_by_user: dict[str, list[Any]] = {}
    for membership in service.user_group_dao.list_all():
        groups_by_user.setdefault(membership.user_id, []).append(membership)
    bindings_by_group: dict[str, list[Any]] = {}
    for binding in service.group_binding_dao.list_all():
        bindings_by_group.setdefault(binding.group_id, []).append(binding)
    items: list[dict[str, Any]] = []
    for user in service.user_dao.list(query=query, status=status):
        data = _user_public(user)
        data["roles"] = [
            {
                "name": role_names.get(binding.role_id, binding.role_id),
                "scopeType": binding.scope_type,
                "scopeClusterId": binding.scope_cluster_id,
            }
            for binding in bindings_by_user.get(user.id, [])
        ]
        memberships = groups_by_user.get(user.id, [])
        data["groups"] = [
            {
                "id": membership.group_id,
                "name": group_names.get(membership.group_id, membership.group_id),
                "source": membership.source,
            }
            for membership in memberships
        ]
        # Roles the user only holds through a group membership, so the UI can
        # show them separately from the user's own direct bindings.
        data["inheritedRoles"] = [
            {
                "name": role_names.get(binding.role_id, binding.role_id),
                "scopeType": binding.scope_type,
                "scopeClusterId": binding.scope_cluster_id,
                "scopeResourceType": binding.scope_resource_type,
                "scopeResourceId": binding.scope_resource_id,
                "groupId": membership.group_id,
                "groupName": group_names.get(membership.group_id, membership.group_id),
            }
            for membership in memberships
            for binding in bindings_by_group.get(membership.group_id, [])
        ]
        items.append(data)
    return items


@router.post("/users", status_code=201)
async def create_user(body: UserCreateRequest) -> dict[str, Any]:
    user = default_service().create_user(
        username=body.username,
        password=body.password,
        display_name=body.display_name,
        email=body.email,
    )
    return _user_public(user)


@router.get("/users/{user_id}")
async def get_user(user_id: str) -> dict[str, Any]:
    user = default_service().user_dao.get(user_id)
    if user is None:
        raise NotFoundError("user not found")
    return _user_public(user)


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdateRequest) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    return _user_public(default_service().update_user(user_id, **updates))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, request: Request) -> None:
    default_service().delete_user(user_id, actor=_principal(request))


@router.post("/users/{user_id}/password", status_code=204)
async def reset_password(user_id: str, body: ResetPasswordRequest) -> None:
    default_service().reset_password(user_id, body.new_password)


@router.get("/users/{user_id}/groups")
async def user_groups(user_id: str) -> list[dict[str, Any]]:
    return _jsonify(default_service().user_group_dao.list_for_user(user_id))


@router.put("/users/{user_id}/groups", status_code=204)
async def set_user_groups(user_id: str, body: MembersRequest) -> None:
    service = default_service()
    current = {membership.group_id for membership in service.user_group_dao.list_for_user(user_id)}
    target = set(body.user_ids)
    for group_id in target - current:
        service.add_group_member(group_id, user_id)
    for group_id in current - target:
        service.remove_group_member(group_id, user_id)


@router.get("/users/{user_id}/role-bindings")
async def user_bindings(user_id: str) -> list[dict[str, Any]]:
    return _jsonify(default_service().user_binding_dao.list_for_user(user_id))


@router.post("/users/{user_id}/role-bindings", status_code=201)
async def add_user_binding(user_id: str, body: BindingRequest, request: Request) -> dict[str, Any]:
    actor = _principal(request)
    record = default_service().add_user_binding(
        user_id,
        body.role_id,
        scope_type=body.scope_type,
        scope_cluster_id=body.scope_cluster_id,
        scope_resource_type=body.scope_resource_type,
        scope_resource_id=body.scope_resource_id,
        granted_by=actor.user_id,
    )
    return _binding_json(record)


@router.delete("/users/{user_id}/role-bindings/{binding_id}", status_code=204)
async def remove_user_binding(user_id: str, binding_id: str) -> None:
    default_service().remove_user_binding(user_id, binding_id)


# --- groups -----------------------------------------------------------------
@router.get("/groups")
async def list_groups() -> list[dict[str, Any]]:
    service = default_service()
    role_names = {role.id: role.name for role in service.role_dao.list()}
    bindings_by_group: dict[str, list[Any]] = {}
    for binding in service.group_binding_dao.list_all():
        bindings_by_group.setdefault(binding.group_id, []).append(binding)
    items: list[dict[str, Any]] = []
    for group in service.group_dao.list():
        data = _jsonify(group)
        data["roles"] = [
            {
                "name": role_names.get(binding.role_id, binding.role_id),
                "scopeType": binding.scope_type,
                "scopeClusterId": binding.scope_cluster_id,
                "scopeResourceType": binding.scope_resource_type,
                "scopeResourceId": binding.scope_resource_id,
            }
            for binding in bindings_by_group.get(group.id, [])
        ]
        items.append(data)
    return items


@router.post("/groups", status_code=201)
async def create_group(body: GroupCreateRequest) -> dict[str, Any]:
    return _jsonify(default_service().create_group(name=body.name, description=body.description))


@router.get("/groups/{group_id}")
async def get_group(group_id: str) -> dict[str, Any]:
    group = default_service().group_dao.get(group_id)
    if group is None:
        raise NotFoundError("group not found")
    return _jsonify(group)


@router.patch("/groups/{group_id}")
async def update_group(group_id: str, body: GroupUpdateRequest) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    return _jsonify(default_service().update_group(group_id, **updates))


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str) -> None:
    default_service().delete_group(group_id)


@router.get("/groups/{group_id}/members")
async def list_group_members(group_id: str) -> list[dict[str, Any]]:
    return _jsonify(default_service().user_group_dao.list_for_group(group_id))


@router.put("/groups/{group_id}/members", status_code=204)
async def set_group_members(group_id: str, body: MembersRequest) -> None:
    service = default_service()
    current = {membership.user_id for membership in service.user_group_dao.list_for_group(group_id)}
    target = set(body.user_ids)
    for user_id in target - current:
        service.add_group_member(group_id, user_id)
    for user_id in current - target:
        service.remove_group_member(group_id, user_id)


@router.get("/groups/{group_id}/role-bindings")
async def group_bindings(group_id: str) -> list[dict[str, Any]]:
    return _jsonify(default_service().group_binding_dao.list_for_group(group_id))


@router.post("/groups/{group_id}/role-bindings", status_code=201)
async def add_group_binding(group_id: str, body: BindingRequest, request: Request) -> dict[str, Any]:
    actor = _principal(request)
    record = default_service().add_group_binding(
        group_id,
        body.role_id,
        scope_type=body.scope_type,
        scope_cluster_id=body.scope_cluster_id,
        scope_resource_type=body.scope_resource_type,
        scope_resource_id=body.scope_resource_id,
        granted_by=actor.user_id,
    )
    return _binding_json(record)


@router.delete("/groups/{group_id}/role-bindings/{binding_id}", status_code=204)
async def remove_group_binding(group_id: str, binding_id: str) -> None:
    default_service().remove_group_binding(group_id, binding_id)


# --- roles ------------------------------------------------------------------
@router.get("/roles")
async def list_roles() -> list[dict[str, Any]]:
    return [_role_json(role) for role in default_service().role_dao.list()]


@router.post("/roles", status_code=201)
async def create_role(body: RoleCreateRequest) -> dict[str, Any]:
    role = default_service().create_role(
        name=body.name, description=body.description, permissions=set(body.permissions)
    )
    return _role_json(role)


@router.get("/roles/{role_id}")
async def get_role(role_id: str) -> dict[str, Any]:
    role = default_service().role_dao.get(role_id)
    if role is None:
        raise NotFoundError("role not found")
    return _role_json(role)


@router.patch("/roles/{role_id}")
async def update_role(role_id: str, body: dict[str, Any]) -> dict[str, Any]:
    role = default_service().require_role(role_id)
    if role.is_builtin:
        raise DomainValidationError("built-in roles cannot be edited")
    updates = {key: body[key] for key in ("name", "description") if key in body}
    return _jsonify(default_service().role_dao.save(_replace(role, updates)))


def _replace(record: RoleRecord, updates: dict[str, Any]) -> RoleRecord:
    for key, value in updates.items():
        setattr(record, key, value)
    return record


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: str) -> None:
    role = default_service().require_role(role_id)
    if role.is_builtin:
        raise DomainValidationError("built-in roles cannot be deleted")
    default_service().role_dao.delete(role_id)


@router.put("/roles/{role_id}/permissions", status_code=204)
async def set_role_permissions(role_id: str, body: RolePermissionsRequest) -> None:
    default_service().set_role_permissions(role_id, set(body.permissions))


# --- identity providers -----------------------------------------------------
@router.get("/identity-providers")
async def list_identity_providers() -> list[dict[str, Any]]:
    return [_provider_public(record) for record in default_service().identity_provider_dao.list()]


@router.post("/identity-providers", status_code=201)
async def create_identity_provider(body: ProviderRequest) -> dict[str, Any]:
    service = default_service()
    record = service.identity_provider_dao.create(
        IdentityProviderRecord(
            type=body.type,
            name=body.name,
            enabled=body.enabled,
            is_default=body.is_default,
            config=body.config,
            sync_mode=body.sync_mode,
        )
    )
    if body.secret:
        record.secret_encrypted = service.encrypt_provider_secret(body.secret)
        record = service.identity_provider_dao.save(record)
    return _provider_public(record)


@router.get("/identity-providers/{provider_id}")
async def get_identity_provider(provider_id: str) -> dict[str, Any]:
    record = default_service().identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    return _provider_public(record)


@router.patch("/identity-providers/{provider_id}")
async def update_identity_provider(provider_id: str, body: ProviderRequest) -> dict[str, Any]:
    service = default_service()
    record = service.identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    record.type = body.type
    record.name = body.name
    record.enabled = body.enabled
    record.is_default = body.is_default
    record.config = body.config
    record.sync_mode = body.sync_mode
    if body.secret:
        record.secret_encrypted = service.encrypt_provider_secret(body.secret)
    return _provider_public(service.identity_provider_dao.save(record))


@router.delete("/identity-providers/{provider_id}", status_code=204)
async def delete_identity_provider(provider_id: str) -> None:
    default_service().delete_identity_provider(provider_id)


@router.post("/identity-providers/{provider_id}/test")
async def test_identity_provider(provider_id: str) -> dict[str, Any]:
    service = default_service()
    record = service.identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    provider = get_provider(record, secret=service.decrypt_provider_secret(record.secret_encrypted))
    result = await provider.test_connection()
    return {"ok": result.ok, "detail": result.detail}


@router.post("/identity-providers/{provider_id}/sync")
async def sync_identity_provider(provider_id: str) -> dict[str, Any]:
    service = default_service()
    record = service.identity_provider_dao.get(provider_id)
    if record is None:
        raise NotFoundError("identity provider not found")
    return await service.sync_directory(provider_id)


# --- audit ------------------------------------------------------------------
@router.get("/audit-logs")
async def list_audit_logs(
    actor_user_id: str | None = None,
    event_type: str | None = None,
    result: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    dao = default_service().audit_dao
    filters = {
        "actor_user_id": actor_user_id,
        "event_type": event_type,
        "result": result,
        "since": since,
        "until": until,
    }
    return {
        "items": _jsonify(dao.list(**filters, limit=limit, offset=offset)),
        "total": dao.count(**filters),
        "limit": limit,
        "offset": offset,
    }


# --- system: stored-secret master key ---------------------------------------
def _provider_secret_count(service: Any) -> int:
    return sum(1 for record in service.identity_provider_dao.list() if record.secret_encrypted)


@router.get("/system/secret-key")
async def get_secret_key_status() -> dict[str, Any]:
    service = default_service()
    data = master_key.status(get_settings())
    data["providerSecretCount"] = _provider_secret_count(service)
    return data


@router.post("/system/secret-key/rotate")
async def rotate_secret_key(body: RotateSecretKeyRequest, request: Request) -> dict[str, Any]:
    service = default_service()
    actor = _principal(request)
    result = master_key.rotate_master_key(
        get_settings(), body.new_key, provider_dao=service.identity_provider_dao
    )
    service.audit(
        "secret_key_rotated",
        actor=actor,
        target_type="system",
        target_id="secret-key",
        detail={"rotatedSecrets": result.get("rotatedSecrets", 0), "fingerprint": result["fingerprint"]},
    )
    result["providerSecretCount"] = _provider_secret_count(service)
    return result


@router.delete("/system/secret-key/old")
async def clear_old_secret_keys(request: Request) -> dict[str, Any]:
    service = default_service()
    actor = _principal(request)
    result = master_key.clear_old_keys(get_settings())
    service.audit("secret_key_old_cleared", actor=actor, target_type="system", target_id="secret-key")
    result["providerSecretCount"] = _provider_secret_count(service)
    return result


# --- access management ------------------------------------------------------
def _list_access(scope_cluster_id: str | None, scope_resource_id: str | None, service) -> list[dict[str, Any]]:
    user_bindings = [
        binding
        for binding in service.user_binding_dao.list_all()
        if binding.scope_cluster_id == scope_cluster_id
        and (scope_resource_id is None or binding.scope_resource_id == scope_resource_id)
    ]
    group_bindings = [
        binding
        for binding in service.group_binding_dao.list_all()
        if binding.scope_cluster_id == scope_cluster_id
        and (scope_resource_id is None or binding.scope_resource_id == scope_resource_id)
    ]
    return [{"subjectType": "user", **_binding_json(binding)} for binding in user_bindings] + [
        {"subjectType": "group", **_binding_json(binding)} for binding in group_bindings
    ]


@router.get("/clusters/{cluster_id}/access")
async def list_cluster_access(cluster_id: str) -> list[dict[str, Any]]:
    return _list_access(cluster_id, None, default_service())


@router.post("/clusters/{cluster_id}/access", status_code=201)
async def grant_cluster_access(cluster_id: str, body: AccessRequest, request: Request) -> dict[str, Any]:
    service = default_service()
    actor = _principal(request)
    if body.subject_type == "group":
        return _binding_json(
            service.add_group_binding(
                body.subject_id,
                body.role_id,
                scope_type="cluster",
                scope_cluster_id=cluster_id,
                granted_by=actor.user_id,
            )
        )
    return _binding_json(
        service.add_user_binding(
            body.subject_id, body.role_id, scope_type="cluster", scope_cluster_id=cluster_id, granted_by=actor.user_id
        )
    )


@router.delete("/clusters/{cluster_id}/access/{binding_id}", status_code=204)
async def revoke_cluster_access(cluster_id: str, binding_id: str) -> None:
    service = default_service()
    for binding in service.user_binding_dao.list_all():
        if binding.id == binding_id:
            service.remove_user_binding(binding.user_id, binding_id)
            return
    for binding in service.group_binding_dao.list_all():
        if binding.id == binding_id:
            service.remove_group_binding(binding.group_id, binding_id)
            return
    raise NotFoundError("binding not found")
