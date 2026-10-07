"""Authentication and authorization service.

Orchestrates users, groups, roles, bindings and sessions on top of the DAO
layer. Routers and middleware call this module; they never touch a Row or a
Session directly. Design references: sections 5, 7 and 8.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from llm_d_bench.auth.contracts import Principal
from llm_d_bench.auth.master_key import resolve_master_key
from llm_d_bench.auth.permissions import (
    ALL_PERMISSIONS,
    BUILTIN_ROLE_ADMIN,
    BUILTIN_ROLE_END_USER,
    BUILTIN_ROLE_MAINTAINER,
    BUILTIN_ROLE_PERMISSIONS,
)
from llm_d_bench.auth.principal import build_principal
from llm_d_bench.auth.providers.base import ExternalIdentity, IdentityProviderError
from llm_d_bench.auth.providers.registry import get_provider, provider_class
from llm_d_bench.auth.records import (
    AuditLogRecord,
    GroupRecord,
    GroupRoleBindingRecord,
    RoleRecord,
    SessionRecord,
    UserGroupRecord,
    UserRecord,
    UserRoleBindingRecord,
    utcnow,
)
from llm_d_bench.auth.scope import cluster_reachable
from llm_d_bench.auth.security import (
    SecretCipher,
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from llm_d_bench.auth.settings import AuthSettings, get_settings
from llm_d_bench.core.exceptions import (
    ConflictError,
    DomainValidationError,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
)
from llm_d_bench.db.dao.audit_log import AuditLogDao
from llm_d_bench.db.dao.group import GroupDao, GroupRoleBindingDao, UserGroupDao
from llm_d_bench.db.dao.identity_provider import IdentityGroupMappingDao, IdentityProviderDao
from llm_d_bench.db.dao.role import RoleDao, RolePermissionDao, UserRoleBindingDao
from llm_d_bench.db.dao.session import SessionDao
from llm_d_bench.db.dao.user import UserDao

MIN_PASSWORD_LENGTH = 12
_SPECIAL = set("!@#$%^&*()-_=+[]{};:,.<>?/\\|`~\"'")

#: Resource types that may be shared with a resource-scoped role binding.
SHARE_RESOURCE_TYPES = frozenset({"deployment_run", "deployment_execution"})


@dataclass(frozen=True)
class LoginResult:
    user: UserRecord
    token: str
    principal: Principal


def _group_name_from_dn(dn: str) -> str:
    """Best-effort display name (first RDN value) for an LDAP group DN."""
    first = dn.split(",", 1)[0].strip()
    return first.split("=", 1)[1].strip() if "=" in first else dn


def validate_password_strength(password: str, *, username: str = "") -> None:
    """Enforce the configurable baseline password policy (design section 5.2)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise DomainValidationError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if username and password.lower() == username.lower():
        raise DomainValidationError("password must not equal the username")
    categories = sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(c in _SPECIAL for c in password),
        )
    )
    if categories < 3:
        raise DomainValidationError("password must mix at least 3 of: lower, upper, digit, symbol")


def protected_admin_username() -> str:
    """Username of the first-run admin account that must not be deleted or re-roled."""
    return (get_settings().initial_admin_username or "admin").strip().lower()


def is_protected_user(user: UserRecord) -> bool:
    return user.username.strip().lower() == protected_admin_username()


class AuthService:
    def __init__(
        self,
        settings: AuthSettings | None = None,
        *,
        user_dao: UserDao | None = None,
        session_dao: SessionDao | None = None,
        audit_dao: AuditLogDao | None = None,
        user_group_dao: UserGroupDao | None = None,
        group_dao: GroupDao | None = None,
        user_binding_dao: UserRoleBindingDao | None = None,
        group_binding_dao: GroupRoleBindingDao | None = None,
        role_dao: RoleDao | None = None,
        role_permission_dao: RolePermissionDao | None = None,
        identity_provider_dao: IdentityProviderDao | None = None,
        identity_mapping_dao: IdentityGroupMappingDao | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.user_dao = user_dao or UserDao()
        self.session_dao = session_dao or SessionDao()
        self.audit_dao = audit_dao or AuditLogDao()
        self.user_group_dao = user_group_dao or UserGroupDao()
        self.group_dao = group_dao or GroupDao()
        self.user_binding_dao = user_binding_dao or UserRoleBindingDao()
        self.group_binding_dao = group_binding_dao or GroupRoleBindingDao()
        self.role_dao = role_dao or RoleDao()
        self.role_permission_dao = role_permission_dao or RolePermissionDao()
        self.identity_provider_dao = identity_provider_dao or IdentityProviderDao()
        self.identity_mapping_dao = identity_mapping_dao or IdentityGroupMappingDao()

    # --- external providers (LDAP, future OIDC) ------------------------------
    def secret_cipher(self) -> SecretCipher:
        resolved = resolve_master_key(self.settings)
        if not resolved.primary:
            raise DomainValidationError("LENS_SECRET_KEY is required to read provider secrets")
        return SecretCipher(resolved.primary, old=resolved.old)

    def decrypt_provider_secret(self, secret_encrypted: str | None) -> str | None:
        if not secret_encrypted:
            return None
        return self.secret_cipher().decrypt(secret_encrypted)

    def encrypt_provider_secret(self, plaintext: str | None) -> str | None:
        if not plaintext:
            return None
        return self.secret_cipher().encrypt(plaintext)

    async def authenticate_external(
        self,
        username: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
        remember: bool = False,
    ) -> LoginResult:
        """Try enabled external providers and JIT-provision on success."""
        enabled = [
            record
            for record in self.identity_provider_dao.list_enabled()
            if record.type != "local"
            and (provider_class(record.type) is None or provider_class(record.type).capabilities.password_auth)
        ]
        for record in enabled:
            try:
                provider = get_provider(record, secret=self.decrypt_provider_secret(record.secret_encrypted))
                identity = await provider.authenticate(username, password)
            except IdentityProviderError as error:
                self.audit(
                    "login_failed",
                    result="failure",
                    actor_username=username,
                    ip=ip,
                    detail={"reason": str(error)},
                )
                continue
            if identity is None:
                continue
            user = self._upsert_external_user(record, identity)
            self._sync_external_groups(record, user, identity)
            self.user_dao.mark_login(user.id)
            refreshed = self.user_dao.get(user.id) or user
            return self._issue_session(refreshed, ip=ip, user_agent=user_agent, remember=remember)
        self.audit("login_failed", result="failure", actor_username=username, ip=ip, detail={"reason": "external"})
        raise UnauthenticatedError("invalid username or password")

    async def login(
        self,
        username: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
        remember: bool = False,
    ) -> LoginResult:
        """Authenticate ``username`` against whichever source owns that account.

        There is no global "auth mode" switch: each username is checked the
        one way that actually applies to it (design section 5/8).

        * Already provisioned locally (``auth_source == "local"``, including
          the bootstrap admin and any account an admin created by hand) →
          verified against its local password hash.
        * Already provisioned from a directory (``auth_source`` is a provider
          type, set by a prior login or directory sync) → verified against
          that same identity provider only; the local password hash (which
          external accounts never have) is never consulted.
        * Not provisioned yet → a first-time directory login: every enabled
          external provider is tried in turn, and the first match
          JIT-provisions the local account.
        """
        user = self.user_dao.get_by_username(username)
        if user is not None and user.auth_source == "local":
            return self.authenticate(username, password, ip=ip, user_agent=user_agent, remember=remember)
        if user is not None:
            return await self._authenticate_existing_external_user(
                user, password, ip=ip, user_agent=user_agent, remember=remember
            )
        return await self.authenticate_external(username, password, ip=ip, user_agent=user_agent, remember=remember)

    async def _authenticate_existing_external_user(
        self,
        user: UserRecord,
        password: str,
        *,
        ip: str | None,
        user_agent: str | None,
        remember: bool,
    ) -> LoginResult:
        """Verify a previously JIT-provisioned directory user against its own provider."""
        if user.status == "disabled":
            self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": "disabled"})
            raise UnauthenticatedError("invalid username or password")
        now = utcnow()
        if user.locked_until is not None and user.locked_until > now:
            self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": "locked"})
            raise UnauthenticatedError("account is temporarily locked")

        record = self.identity_provider_dao.get(user.provider_id) if user.provider_id else None
        identity = None
        if record is not None and record.enabled:
            try:
                provider = get_provider(record, secret=self.decrypt_provider_secret(record.secret_encrypted))
                identity = await provider.authenticate(user.username, password)
            except IdentityProviderError as error:
                self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": str(error)})
                identity = None

        if identity is None:
            self._register_failure(user)
            self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": "bad_password"})
            raise UnauthenticatedError("invalid username or password")

        refreshed_user = self._upsert_external_user(record, identity)
        self._sync_external_groups(record, refreshed_user, identity)
        self.user_dao.mark_login(refreshed_user.id)
        refreshed = self.user_dao.get(refreshed_user.id) or refreshed_user
        return self._issue_session(refreshed, ip=ip, user_agent=user_agent, remember=remember)

    def _upsert_external_user(self, provider, identity: ExternalIdentity) -> UserRecord:
        user = self.user_dao.get_by_username(identity.username) or self.user_dao.get_by_external(
            provider.id, identity.external_id
        )
        if user is None:
            user = UserRecord(
                username=identity.username,
                display_name=identity.display_name or identity.username,
                email=identity.email or None,
                auth_source=provider.type,
                provider_id=provider.id,
                external_id=identity.external_id,
            )
            return self.user_dao.create(user)
        user.display_name = identity.display_name or user.display_name
        user.email = identity.email or user.email
        user.auth_source = provider.type
        user.provider_id = provider.id
        user.external_id = identity.external_id
        user.principal_version = (user.principal_version or 1) + 1
        return self.user_dao.save(user)

    def _upsert_external_group(self, provider, external_id: str, name: str) -> GroupRecord:
        """Create or refresh the Lens group mirroring a directory group."""
        existing = self.group_dao.get_by_external(provider.id, external_id)
        if existing is not None:
            if name and existing.name != name:
                existing.name = name
                return self.group_dao.save(existing)
            return existing
        return self.group_dao.create(
            GroupRecord(
                name=name or external_id,
                source=provider.type,
                provider_id=provider.id,
                external_id=external_id,
            )
        )

    def _sync_external_groups(self, provider, user: UserRecord, identity: ExternalIdentity) -> None:
        """Reflect the user's directory groups as Lens group memberships on login.

        Directory groups are materialized as ordinary ``groups`` rows with
        ``source=ldap`` so they can be granted roles and shared to like any other
        group. Memberships synced here use ``source=external`` and are removed
        again when the directory no longer reports them (manual ones are kept).
        """
        synced: set[str] = set()
        for external_id in identity.groups:
            group = self._upsert_external_group(provider, external_id, _group_name_from_dn(external_id))
            synced.add(group.id)
            current = {membership.group_id for membership in self.user_group_dao.list_for_user(user.id)}
            if group.id not in current:
                self.user_group_dao.add(UserGroupRecord(user_id=user.id, group_id=group.id, source="external"))
                self.group_dao.bump_authz_version(group.id)
        for membership in self.user_group_dao.list_for_user(user.id):
            if membership.source != "external":
                continue
            group = self.group_dao.get(membership.group_id)
            if group is not None and group.provider_id == provider.id and membership.group_id not in synced:
                self.user_group_dao.remove(user.id, membership.group_id)
                self.group_dao.bump_authz_version(membership.group_id)

    # --- audit ---------------------------------------------------------------
    def audit(
        self,
        event_type: str,
        *,
        result: str = "success",
        actor: UserRecord | None = None,
        actor_user_id: str | None = None,
        actor_username: str = "",
        permission: str | None = None,
        method: str | None = None,
        path: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        cluster_id: str | None = None,
        detail: dict | None = None,
        ip: str | None = None,
    ) -> None:
        try:
            self.audit_dao.append(
                AuditLogRecord(
                    event_type=event_type,
                    result=result,
                    actor_user_id=actor.id if actor else actor_user_id,
                    actor_username=actor.username if actor else actor_username,
                    permission=permission,
                    method=method,
                    path=path,
                    target_type=target_type,
                    target_id=target_id,
                    cluster_id=cluster_id,
                    detail=detail,
                    ip=ip,
                )
            )
        except Exception:  # noqa: BLE001 - audit must never break the request
            import logging

            logging.getLogger(__name__).exception("failed to write audit log (%s)", event_type)

    # --- principal -----------------------------------------------------------
    def principal_for(self, user: UserRecord) -> Principal:
        return build_principal(
            user,
            user_group_dao=self.user_group_dao,
            group_dao=self.group_dao,
            user_binding_dao=self.user_binding_dao,
            group_binding_dao=self.group_binding_dao,
            role_dao=self.role_dao,
            role_permission_dao=self.role_permission_dao,
        )

    # --- authentication ------------------------------------------------------
    def authenticate(
        self,
        username: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
        remember: bool = False,
    ) -> LoginResult:
        user = self.user_dao.get_by_username(username)
        if user is None:
            self.audit(
                "login_failed",
                result="failure",
                actor_username=username,
                ip=ip,
                detail={"reason": "unknown_user"},
            )
            raise UnauthenticatedError("invalid username or password")
        if user.status == "disabled":
            self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": "disabled"})
            raise UnauthenticatedError("invalid username or password")
        now = utcnow()
        if user.locked_until is not None and user.locked_until > now:
            self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": "locked"})
            raise UnauthenticatedError("account is temporarily locked")

        if not self._verify_credentials(user, password):
            self._register_failure(user)
            self.audit("login_failed", result="failure", actor=user, ip=ip, detail={"reason": "bad_password"})
            raise UnauthenticatedError("invalid username or password")

        self.user_dao.mark_login(user.id)
        refreshed = self.user_dao.get(user.id) or user
        return self._issue_session(refreshed, ip=ip, user_agent=user_agent, remember=remember)

    def _verify_credentials(self, user: UserRecord, password: str) -> bool:
        if user.password_hash:
            return verify_password(password, user.password_hash)
        # External-provider users (LDAP/OIDC) are handled by their provider;
        # without one configured there is nothing to verify here.
        return False

    def _register_failure(self, user: UserRecord) -> None:
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= self.settings.login_max_failures:
            user.locked_until = utcnow() + timedelta(seconds=self.settings.login_lockout_seconds)
        self.user_dao.save(user)

    def _issue_session(
        self, user: UserRecord, *, ip: str | None, user_agent: str | None, remember: bool = False
    ) -> LoginResult:
        now = utcnow()
        token = generate_session_token()
        ttl = self.settings.remember_session_ttl_seconds if remember else self.settings.session_ttl_seconds
        idle = self.settings.remember_session_idle_seconds if remember else self.settings.session_idle_seconds
        self.session_dao.create(
            SessionRecord(
                user_id=user.id,
                token_hash=hash_session_token(token),
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(seconds=ttl),
                idle_expires_at=now + timedelta(seconds=idle),
                auth_source=user.auth_source,
                ip=ip,
                user_agent=user_agent,
            )
        )
        self._enforce_session_limit(user, token=token, ip=ip)
        self.audit("login", actor=user, ip=ip)
        return LoginResult(user=user, token=token, principal=self.principal_for(user))

    def _enforce_session_limit(self, user: UserRecord, *, token: str, ip: str | None) -> None:
        """Keep one (or ``session_max_per_user``) active session(s) per account.

        With ``session_single_active`` a new login revokes every other session for
        the account, so signing in elsewhere kicks the previous device out.
        """
        if not self.settings.session_single_active:
            self.session_dao.trim_user_sessions(user.id, self.settings.session_max_per_user)
            return
        current = self.session_dao.get_by_token_hash(hash_session_token(token))
        if current is None:
            return
        superseded = self.session_dao.revoke_user_sessions(user.id, keep_session_id=current.id)
        self.user_dao.bump_principal_version(user.id)
        if superseded:
            self.audit(
                "session_revoked",
                actor=user,
                ip=ip,
                target_type="user",
                target_id=user.id,
                detail={"reason": "superseded by new login", "count": superseded},
            )

    def validate(self, raw_token: str) -> Principal | None:
        if not raw_token:
            return None
        session = self.session_dao.get_by_token_hash(hash_session_token(raw_token))
        if session is None or session.revoked_at is not None:
            return None
        now = utcnow()
        if session.expires_at is None or session.idle_expires_at is None:
            return None
        if now >= session.expires_at or now >= session.idle_expires_at:
            return None
        user = self.user_dao.get(session.user_id)
        if user is None or user.status != "active":
            return None
        stale = (
            session.last_seen_at is None
            or (now - session.last_seen_at).total_seconds() > self.settings.session_touch_interval_seconds
        )
        if stale:
            self.session_dao.touch(
                session.id,
                idle_expires_at=now + timedelta(seconds=self.settings.session_idle_seconds),
                last_seen_at=now,
            )
        return self.principal_for(user)

    def principal_from_token(self, raw_token: str) -> Principal:
        principal = self.validate(raw_token)
        if principal is None:
            raise UnauthenticatedError("session is missing, expired or revoked")
        return principal

    def principal_for_request(
        self,
        *,
        token: str | None,
        internal_secret_ok: bool,
        internal_principal_id: str | None,
    ) -> Principal:
        """Resolve a principal from the browser session or an internal assertion."""
        if token:
            return self.principal_from_token(token)
        if internal_secret_ok and internal_principal_id:
            user = self.user_dao.get(internal_principal_id)
            if user is None or user.status != "active":
                raise UnauthenticatedError("unknown principal")
            return self.principal_for(user)
        raise UnauthenticatedError("authentication required")

    def logout(self, raw_token: str) -> None:
        if not raw_token:
            return
        session = self.session_dao.get_by_token_hash(hash_session_token(raw_token))
        if session is not None:
            self.session_dao.revoke(session.id)
            self.audit("logout", target_type="user", target_id=session.user_id)

    # --- sessions ------------------------------------------------------------
    def list_sessions(self, user_id: str, *, include_revoked: bool = False) -> list[SessionRecord]:
        return self.session_dao.list_for_user(user_id, include_revoked=include_revoked)

    def revoke_session(self, session_id: str, *, actor: Principal | None = None, allow_any: bool = False) -> None:
        session = self.session_dao.get(session_id)
        if session is None:
            raise NotFoundError("session not found")
        if actor is not None and session.user_id != actor.user_id and not allow_any:
            raise ForbiddenError("cannot revoke another user's session")
        self.session_dao.revoke(session_id)
        self.audit(
            "session_revoked",
            actor_user_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else "",
            target_type="session",
            target_id=session_id,
        )

    def revoke_user_sessions(self, user_id: str) -> None:
        self.session_dao.revoke_user_sessions(user_id)
        self.user_dao.bump_principal_version(user_id)

    def sweep_sessions(self) -> int:
        return self.session_dao.sweep(revoked_retention_seconds=self.settings.session_revoked_retention_seconds)

    # --- passwords -----------------------------------------------------------
    def change_password(
        self, user_id: str, old_password: str, new_password: str, *, keep_token: str | None = None
    ) -> None:
        user = self.user_dao.get(user_id)
        if user is None:
            raise NotFoundError("user not found")
        if user.password_hash and not verify_password(old_password, user.password_hash):
            raise UnauthenticatedError("current password is incorrect")
        validate_password_strength(new_password, username=user.username)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        user.must_change_password = False
        user.failed_login_count = 0
        user.locked_until = None
        user.principal_version = (user.principal_version or 1) + 1
        self.user_dao.save(user)
        # Revoke every other session, but keep the caller's session so they can
        # continue after changing an expired/default password (design section 5.2).
        keep_session_id = None
        if keep_token:
            session = self.session_dao.get_by_token_hash(hash_session_token(keep_token))
            if session is not None and session.user_id == user_id:
                keep_session_id = session.id
        self.session_dao.revoke_user_sessions(user_id, keep_session_id=keep_session_id)
        self.audit("password_changed", actor=user)

    # --- bootstrap & built-in roles -----------------------------------------
    def ensure_builtin_roles(self) -> None:
        for name in (BUILTIN_ROLE_ADMIN, BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_END_USER):
            role = self.role_dao.get_by_name(name)
            if role is None:
                role = self.role_dao.create(RoleRecord(name=name, description=f"built-in {name}", is_builtin=True))
            self.role_permission_dao.set_for_role(role.id, BUILTIN_ROLE_PERMISSIONS[name])

    def user_count(self) -> int:
        return len(self.user_dao.list())

    def bootstrap_admin(self, username: str, password: str) -> UserRecord:
        if self.user_count() > 0:
            raise ConflictError("bootstrap is only allowed while no users exist")
        self.ensure_builtin_roles()
        user = self.create_user(username=username, password=password, display_name=username)
        admin_role = self.require_role(BUILTIN_ROLE_ADMIN)
        self.user_binding_dao.create(UserRoleBindingRecord(user_id=user.id, role_id=admin_role.id, scope_type="global"))
        self.user_dao.bump_principal_version(user.id)
        self.audit("bootstrap", actor=user)
        return user

    # --- users ---------------------------------------------------------------
    def create_user(
        self,
        *,
        username: str,
        password: str | None = None,
        display_name: str = "",
        email: str | None = None,
        auth_source: str = "local",
        provider_id: str | None = None,
        external_id: str | None = None,
        must_change_password: bool = False,
        validate_password: bool = True,
    ) -> UserRecord:
        username = username.strip()
        if not username:
            raise DomainValidationError("username is required")
        if self.user_dao.get_by_username(username) is not None:
            raise ConflictError(f"username already exists: {username}")
        password_hash = None
        if password is not None:
            if validate_password:
                validate_password_strength(password, username=username)
            password_hash = hash_password(password)
        record = UserRecord(
            username=username,
            display_name=display_name or username,
            email=email,
            password_hash=password_hash,
            auth_source=auth_source,
            provider_id=provider_id,
            external_id=external_id,
            must_change_password=must_change_password,
        )
        created = self.user_dao.create(record)
        self.audit("user_created", target_type="user", target_id=created.id, detail={"username": username})
        return created

    def update_user(self, user_id: str, **updates: object) -> UserRecord:
        user = self.user_dao.get(user_id)
        if user is None:
            raise NotFoundError("user not found")
        if is_protected_user(user) and "status" in updates and updates["status"] != user.status:
            raise ConflictError("the built-in admin account cannot be disabled or re-enabled")
        allowed = {"display_name", "email", "status"}
        for key, value in updates.items():
            if key in allowed:
                setattr(user, key, value)
        user.principal_version = (user.principal_version or 1) + 1
        saved = self.user_dao.save(user)
        if saved.status != "active":
            self.session_dao.revoke_user_sessions(user_id)
        self.audit("user_updated", target_type="user", target_id=user_id, detail={"fields": sorted(updates)})
        return saved

    def delete_user(self, user_id: str, *, actor: Principal | None = None) -> None:
        if actor is not None and actor.user_id == user_id:
            raise DomainValidationError("cannot delete yourself")
        user = self.user_dao.get(user_id)
        if user is None:
            raise NotFoundError("user not found")
        if is_protected_user(user):
            raise ConflictError("the built-in admin account cannot be deleted")
        self._guard_last_admin(user_id)
        self.user_dao.delete(user_id)
        self.audit("user_deleted", target_type="user", target_id=user_id, detail={"username": user.username})

    def reset_password(self, user_id: str, new_password: str) -> None:
        user = self.user_dao.get(user_id)
        if user is None:
            raise NotFoundError("user not found")
        if user.auth_source != "local":
            # The directory (LDAP/OIDC) owns this account's password; setting a
            # local one here would let it silently bypass the external provider.
            raise DomainValidationError(
                "this account's password is managed by its external identity provider and cannot be reset in Lens"
            )
        validate_password_strength(new_password, username=user.username)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        user.must_change_password = True
        user.failed_login_count = 0
        user.locked_until = None
        user.principal_version = (user.principal_version or 1) + 1
        self.user_dao.save(user)
        self.session_dao.revoke_user_sessions(user_id)
        self.audit("password_reset", target_type="user", target_id=user_id)

    def _guard_last_admin(self, user_id: str) -> None:
        admin_role = self.role_dao.get_by_name(BUILTIN_ROLE_ADMIN)
        if admin_role is None:
            return
        bindings = [
            binding
            for binding in self.user_binding_dao.list_all()
            if binding.role_id == admin_role.id and binding.scope_type == "global"
        ]
        if len(bindings) <= 1 and any(binding.user_id == user_id for binding in bindings):
            raise ConflictError("cannot remove the last global admin")

    async def sync_directory(self, provider_id: str) -> dict[str, object]:
        """Materialize the directory's groups and members into Lens.

        Every group under the provider's ``group_base_dn`` becomes a Lens group
        (``source=ldap``) and its members become Lens users plus memberships
        (``source=external``). Memberships no longer reported by the directory
        are removed; manually added memberships are kept.
        """
        record = self.identity_provider_dao.get(provider_id)
        if record is None:
            raise NotFoundError("identity provider not found")
        provider = get_provider(record, secret=self.decrypt_provider_secret(record.secret_encrypted))
        descriptors = await provider.list_groups()
        group_count = 0
        member_count = 0
        failed: list[str] = []
        for descriptor in descriptors:
            external_id = str(descriptor.get("external_id") or "")
            if not external_id:
                continue
            try:
                group = self._upsert_external_group(
                    record, external_id, str(descriptor.get("name") or external_id)
                )
                members = await provider.list_group_members(external_id)
            except IdentityProviderError as error:
                failed.append(external_id)
                self.audit(
                    "directory_sync_failed",
                    result="failure",
                    target_type="identity_provider",
                    target_id=provider_id,
                    detail={"group": external_id, "error": str(error)},
                )
                continue
            group_count += 1
            member_ids: set[str] = set()
            for identity in members:
                user = self._upsert_external_user(record, identity)
                member_ids.add(user.id)
                current = {membership.group_id for membership in self.user_group_dao.list_for_user(user.id)}
                if group.id not in current:
                    self.user_group_dao.add(UserGroupRecord(user_id=user.id, group_id=group.id, source="external"))
                    self.group_dao.bump_authz_version(group.id)
                member_count += 1
            for membership in self.user_group_dao.list_for_group(group.id):
                if membership.source == "external" and membership.user_id not in member_ids:
                    self.user_group_dao.remove(membership.user_id, group.id)
                    self.group_dao.bump_authz_version(group.id)
        record.last_sync_at = utcnow()
        self.identity_provider_dao.save(record)
        self.audit(
            "directory_synced",
            target_type="identity_provider",
            target_id=provider_id,
            detail={"groups": group_count, "members": member_count, "failed": failed},
        )
        return {"ok": not failed, "groups": group_count, "members": member_count, "failed": failed}

    def delete_identity_provider(self, provider_id: str) -> None:
        """Delete a provider and every external user/group (and authorization) it produced.

        Deleting the users/groups cascades their role bindings, cluster access,
        group memberships and resource (deployment) shares. Refuses when it would
        remove the last global admin.
        """
        record = self.identity_provider_dao.get(provider_id)
        if record is None:
            raise NotFoundError("identity provider not found")
        external_users = [
            user for user in self.user_dao.list() if user.provider_id == provider_id and user.auth_source != "local"
        ]
        for user in external_users:
            self._guard_last_admin(user.id)
        for user in external_users:
            self.session_dao.revoke_user_sessions(user.id)
            self.user_dao.delete(user.id)
        external_groups = self.group_dao.list_for_provider(provider_id)
        for group in external_groups:
            self.group_dao.delete(group.id)
        self.identity_provider_dao.delete(provider_id)
        self.audit(
            "identity_provider_deleted",
            target_type="identity_provider",
            target_id=provider_id,
            detail={"users": len(external_users), "groups": len(external_groups)},
        )

    # --- groups --------------------------------------------------------------
    def create_group(
        self, *, name: str, description: str = "", source: str = "local", provider_id: str | None = None
    ) -> GroupRecord:
        if self.group_dao.get_by_name(name) is not None:
            raise ConflictError(f"group already exists: {name}")
        return self.group_dao.create(
            GroupRecord(name=name.strip(), description=description, source=source, provider_id=provider_id)
        )

    def update_group(self, group_id: str, **updates: object) -> GroupRecord:
        group = self.group_dao.get(group_id)
        if group is None:
            raise NotFoundError("group not found")
        for key in ("name", "description"):
            if key in updates:
                setattr(group, key, updates[key])
        return self.group_dao.save(group)

    def delete_group(self, group_id: str) -> None:
        if self.group_dao.get(group_id) is None:
            raise NotFoundError("group not found")
        self.group_dao.delete(group_id)

    def _require_local_group(self, group_id: str) -> GroupRecord:
        group = self.group_dao.get(group_id)
        if group is None:
            raise NotFoundError("group not found")
        if group.source != "local":
            raise DomainValidationError("membership of directory groups is managed by the identity provider")
        return group

    def add_group_member(self, group_id: str, user_id: str, *, source: str = "manual") -> None:
        self._require_local_group(group_id)
        if self.user_dao.get(user_id) is None:
            raise NotFoundError("user not found")
        self.user_group_dao.add(UserGroupRecord(user_id=user_id, group_id=group_id, source=source))
        self.group_dao.bump_authz_version(group_id)

    def remove_group_member(self, group_id: str, user_id: str) -> None:
        self._require_local_group(group_id)
        self.user_group_dao.remove(user_id, group_id)
        self.group_dao.bump_authz_version(group_id)

    # --- roles & bindings ----------------------------------------------------
    def require_role(self, name_or_id: str) -> RoleRecord:
        role = self.role_dao.get(name_or_id) or self.role_dao.get_by_name(name_or_id)
        if role is None:
            raise NotFoundError(f"role not found: {name_or_id}")
        return role

    def create_role(self, *, name: str, description: str = "", permissions: set[str] | None = None) -> RoleRecord:
        if name in (BUILTIN_ROLE_ADMIN, BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_END_USER):
            raise ConflictError("cannot create a role with a built-in name")
        if self.role_dao.get_by_name(name) is not None:
            raise ConflictError(f"role already exists: {name}")
        codes = set(permissions or ())
        unknown = codes - set(ALL_PERMISSIONS)
        if unknown:
            raise DomainValidationError(f"unknown permissions: {sorted(unknown)}")
        role = self.role_dao.create(RoleRecord(name=name.strip(), description=description, is_builtin=False))
        self.role_permission_dao.set_for_role(role.id, codes)
        return role

    def set_role_permissions(self, role_id: str, permissions: set[str]) -> None:
        role = self.role_dao.get(role_id)
        if role is None:
            raise NotFoundError("role not found")
        if role.is_builtin:
            raise DomainValidationError("built-in role permissions cannot be modified")
        unknown = set(permissions) - set(ALL_PERMISSIONS)
        if unknown:
            raise DomainValidationError(f"unknown permissions: {sorted(unknown)}")
        self.role_permission_dao.set_for_role(role_id, permissions)
        # Everyone holding this role must re-resolve their permissions.
        for binding in self.user_binding_dao.list_all():
            if binding.role_id == role_id:
                self.user_dao.bump_principal_version(binding.user_id)
        for group_binding in self.group_binding_dao.list_all():
            if group_binding.role_id == role_id:
                self.group_dao.bump_authz_version(group_binding.group_id)

    def add_user_binding(
        self,
        user_id: str,
        role_id: str,
        *,
        scope_type: str = "global",
        scope_cluster_id: str | None = None,
        scope_resource_type: str | None = None,
        scope_resource_id: str | None = None,
        granted_by: str | None = None,
    ) -> UserRoleBindingRecord:
        target = self.user_dao.get(user_id)
        if target is None:
            raise NotFoundError("user not found")
        if is_protected_user(target):
            raise ConflictError("the built-in admin account's roles cannot be changed")
        self.require_role(role_id)
        binding = self.user_binding_dao.create(
            UserRoleBindingRecord(
                user_id=user_id,
                role_id=role_id,
                scope_type=scope_type,
                scope_cluster_id=scope_cluster_id,
                scope_resource_type=scope_resource_type,
                scope_resource_id=scope_resource_id,
                granted_by_user_id=granted_by,
            )
        )
        self.user_dao.bump_principal_version(user_id)
        return binding

    def remove_user_binding(self, user_id: str, binding_id: str) -> None:
        target = self.user_dao.get(user_id)
        if target is None:
            raise NotFoundError("user not found")
        if is_protected_user(target):
            raise ConflictError("the built-in admin account's roles cannot be changed")
        binding = self.user_binding_dao.get(binding_id)
        if binding is None or binding.user_id != user_id:
            raise NotFoundError("binding not found")
        self.user_binding_dao.delete(binding_id)
        self.user_dao.bump_principal_version(user_id)

    def add_group_binding(
        self,
        group_id: str,
        role_id: str,
        *,
        scope_type: str = "global",
        scope_cluster_id: str | None = None,
        scope_resource_type: str | None = None,
        scope_resource_id: str | None = None,
        granted_by: str | None = None,
    ) -> GroupRoleBindingRecord:
        if self.group_dao.get(group_id) is None:
            raise NotFoundError("group not found")
        self.require_role(role_id)
        binding = self.group_binding_dao.create(
            GroupRoleBindingRecord(
                group_id=group_id,
                role_id=role_id,
                scope_type=scope_type,
                scope_cluster_id=scope_cluster_id,
                scope_resource_type=scope_resource_type,
                scope_resource_id=scope_resource_id,
                granted_by_user_id=granted_by,
            )
        )
        self.group_dao.bump_authz_version(group_id)
        return binding

    def remove_group_binding(self, group_id: str, binding_id: str) -> None:
        binding = self.group_binding_dao.get(binding_id)
        if binding is None or binding.group_id != group_id:
            raise NotFoundError("binding not found")
        self.group_binding_dao.delete(binding_id)
        self.group_dao.bump_authz_version(group_id)

    # --- resource sharing (design section 7.7) -------------------------------
    def share_resource(
        self,
        *,
        subject_type: str,
        subject_id: str,
        role_id: str,
        resource_type: str,
        resource_id: str,
        cluster_id: str | None,
        granted_by: str | None = None,
    ) -> UserRoleBindingRecord | GroupRoleBindingRecord:
        """Grant one user/group a role scoped to a single deployment resource.

        Enforces the design's share rules: the grantee must already reach the
        resource's cluster, shares never cross cluster boundaries, and a
        duplicate grant is rejected rather than silently re-created.
        """
        if subject_type not in {"user", "group"}:
            raise DomainValidationError("subject_type must be 'user' or 'group'")
        if resource_type not in SHARE_RESOURCE_TYPES:
            raise DomainValidationError(f"unsupported share resource type: {resource_type}")
        if not resource_id:
            raise DomainValidationError("a share requires a resource id")
        if not cluster_id:
            raise DomainValidationError("a share requires the resource's cluster")
        self.require_role(role_id)
        self._require_share_reachability(subject_type, subject_id, cluster_id)
        if self._find_resource_binding(subject_type, subject_id, role_id, resource_type, resource_id) is not None:
            raise ConflictError("this share already exists")
        scope = {
            "scope_type": "resource",
            "scope_cluster_id": cluster_id,
            "scope_resource_type": resource_type,
            "scope_resource_id": resource_id,
            "granted_by": granted_by,
        }
        if subject_type == "group":
            binding = self.add_group_binding(subject_id, role_id, **scope)
        else:
            binding = self.add_user_binding(subject_id, role_id, **scope)
        self.audit(
            "resource_share_granted",
            target_type=resource_type,
            target_id=resource_id,
            cluster_id=cluster_id,
            detail={"subject_type": subject_type, "subject_id": subject_id, "role_id": role_id},
        )
        return binding

    def revoke_resource_share(
        self, binding_id: str, *, resource_type: str, resource_id: str, cluster_id: str | None
    ) -> None:
        """Revoke only a share belonging to the caller-authorized resource."""
        binding = self.user_binding_dao.get(binding_id)
        subject_type = "user"
        if binding is None:
            binding = self.group_binding_dao.get(binding_id)
            subject_type = "group"
        if (
            binding is None
            or binding.scope_type != "resource"
            or binding.scope_resource_type != resource_type
            or binding.scope_resource_id != resource_id
            or binding.scope_cluster_id != cluster_id
        ):
            raise NotFoundError("binding not found")
        if subject_type == "user":
            subject_id = binding.user_id
            self.remove_user_binding(subject_id, binding_id)
        else:
            subject_id = binding.group_id
            self.remove_group_binding(subject_id, binding_id)
        self.audit(
            "resource_share_revoked",
            target_type=binding.scope_resource_type,
            target_id=binding.scope_resource_id,
            cluster_id=binding.scope_cluster_id,
            detail={"subject_type": subject_type, "subject_id": subject_id},
        )

    def revoke_resource_shares(self, *, resource_type: str, resource_ids: Iterable[str | None]) -> int:
        """Remove every share pointing at these resources (e.g. a deleted deployment).

        Called when the underlying resource no longer exists so a stale share can
        never grant access to a reused id.
        """
        ids = {str(resource_id) for resource_id in resource_ids if resource_id}
        if not ids:
            return 0
        removed = 0
        for binding in list(self.user_binding_dao.list_all()):
            if (
                binding.scope_type == "resource"
                and binding.scope_resource_type == resource_type
                and binding.scope_resource_id in ids
            ):
                self.user_binding_dao.delete(binding.id)
                self.user_dao.bump_principal_version(binding.user_id)
                removed += 1
        for binding in list(self.group_binding_dao.list_all()):
            if (
                binding.scope_type == "resource"
                and binding.scope_resource_type == resource_type
                and binding.scope_resource_id in ids
            ):
                self.group_binding_dao.delete(binding.id)
                self.group_dao.bump_authz_version(binding.group_id)
                removed += 1
        if removed:
            self.audit(
                "resource_shares_revoked",
                target_type=resource_type,
                detail={"resource_ids": sorted(ids), "count": removed},
            )
        return removed

    def list_resource_shares(
        self, resource_type: str, resource_id: str
    ) -> list[tuple[str, UserRoleBindingRecord | GroupRoleBindingRecord]]:
        matches: list[tuple[str, UserRoleBindingRecord | GroupRoleBindingRecord]] = []
        for binding in self.user_binding_dao.list_all():
            if (
                binding.scope_type == "resource"
                and binding.scope_resource_type == resource_type
                and binding.scope_resource_id == resource_id
            ):
                matches.append(("user", binding))
        for binding in self.group_binding_dao.list_all():
            if (
                binding.scope_type == "resource"
                and binding.scope_resource_type == resource_type
                and binding.scope_resource_id == resource_id
            ):
                matches.append(("group", binding))
        return matches

    def _require_share_reachability(self, subject_type: str, subject_id: str, cluster_id: str) -> None:
        if subject_type == "user":
            user = self.user_dao.get(subject_id)
            if user is None:
                raise NotFoundError("user not found")
            if not cluster_reachable(self.principal_for(user), cluster_id):
                raise ForbiddenError("the grantee cannot reach the resource's cluster")
            return
        if self.group_dao.get(subject_id) is None:
            raise NotFoundError("group not found")
        for binding in self.group_binding_dao.list_for_group(subject_id):
            if binding.scope_type == "global" or (
                binding.scope_type == "cluster" and binding.scope_cluster_id == cluster_id
            ):
                return
        raise ForbiddenError("the grantee cannot reach the resource's cluster")

    def _find_resource_binding(
        self, subject_type: str, subject_id: str, role_id: str, resource_type: str, resource_id: str
    ):
        bindings = (
            self.group_binding_dao.list_for_group(subject_id)
            if subject_type == "group"
            else [b for b in self.user_binding_dao.list_all() if b.user_id == subject_id]
        )
        for binding in bindings:
            if (
                binding.role_id == role_id
                and binding.scope_type == "resource"
                and binding.scope_resource_type == resource_type
                and binding.scope_resource_id == resource_id
            ):
                return binding
        return None


def default_service() -> AuthService:
    """Construct a service bound to the current settings.

    A fresh instance is returned each call: DAOs are cheap and this keeps the
    service in step with a test settings override without a global reset.
    """
    return AuthService(settings=get_settings())
