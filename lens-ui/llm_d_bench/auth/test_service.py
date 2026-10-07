"""Service-layer tests for authentication and authorization (design sections 5-7)."""

from __future__ import annotations

import pytest

from llm_d_bench.auth.scope import reachable_clusters
from llm_d_bench.auth.security import hash_session_token
from llm_d_bench.auth.service import AuthService, validate_password_strength
from llm_d_bench.auth.settings import AuthSettings
from llm_d_bench.core.exceptions import (
    ConflictError,
    DomainValidationError,
    UnauthenticatedError,
)
from llm_d_bench.db.dao.cluster import ClusterDao


def _service(**overrides) -> AuthService:
    settings = AuthSettings(
        auth_mode="local",
        session_ttl_seconds=overrides.pop("session_ttl_seconds", 3600),
        session_idle_seconds=overrides.pop("session_idle_seconds", 3600),
        login_max_failures=overrides.pop("login_max_failures", 5),
        login_lockout_seconds=overrides.pop("login_lockout_seconds", 900),
        session_single_active=overrides.pop("session_single_active", True),
    )
    return AuthService(settings)


STRONG = "Correct-Horse-9!"


def test_bootstrap_admin_and_duplicate_conflict():
    service = _service()
    admin = service.bootstrap_admin("root", STRONG)
    assert service.user_dao.get_by_username("root").id == admin.id
    with pytest.raises(ConflictError):
        service.bootstrap_admin("root2", STRONG)

    principal = service.principal_for(admin)
    assert principal.is_global_admin()


def test_authenticate_success_and_failure_paths():
    service = _service()
    service.bootstrap_admin("root", STRONG)
    user = service.create_user(username="alice", password=STRONG)

    result = service.authenticate("alice", STRONG)
    assert result.user.id == user.id
    assert service.validate(result.token) is not None

    with pytest.raises(UnauthenticatedError):
        service.authenticate("alice", "wrong-password")
    with pytest.raises(UnauthenticatedError):
        service.authenticate("nobody", STRONG)

    service.update_user(user.id, status="disabled")
    with pytest.raises(UnauthenticatedError):
        service.authenticate("alice", STRONG)


def test_lockout_after_max_failures():
    service = _service(login_max_failures=3)
    service.bootstrap_admin("root", STRONG)
    service.create_user(username="bob", password=STRONG)
    for _ in range(3):
        with pytest.raises(UnauthenticatedError):
            service.authenticate("bob", "bad-password")
    locked = service.user_dao.get_by_username("bob")
    assert locked.locked_until is not None
    with pytest.raises(UnauthenticatedError, match="locked"):
        service.authenticate("bob", STRONG)


def test_session_logout_and_expiry():
    service = _service()
    service.bootstrap_admin("root", STRONG)
    service.create_user(username="carol", password=STRONG)
    result = service.authenticate("carol", STRONG)
    service.logout(result.token)
    assert service.validate(result.token) is None

    expiring = _service(session_ttl_seconds=0, session_idle_seconds=0)
    user = expiring.create_user(username="dave", password=STRONG)
    assert user is not None
    expired = expiring.authenticate("dave", STRONG)
    assert expiring.validate(expired.token) is None


def test_revoke_user_sessions_bumps_version():
    service = _service()
    user = service.create_user(username="erin", password=STRONG)
    before = service.user_dao.get(user.id).principal_version
    service.authenticate("erin", STRONG)
    service.revoke_user_sessions(user.id)
    assert service.user_dao.get(user.id).principal_version > before


def test_new_login_revokes_previous_session_when_single_active():
    service = _service()
    service.bootstrap_admin("root", STRONG)
    service.create_user(username="gina", password=STRONG)
    first = service.authenticate("gina", STRONG)
    second = service.authenticate("gina", STRONG)
    assert service.validate(first.token) is None
    assert service.validate(second.token) is not None


def test_multiple_sessions_allowed_when_single_active_disabled():
    service = _service(session_single_active=False)
    service.bootstrap_admin("root", STRONG)
    service.create_user(username="hank", password=STRONG)
    first = service.authenticate("hank", STRONG)
    second = service.authenticate("hank", STRONG)
    assert service.validate(first.token) is not None
    assert service.validate(second.token) is not None


def test_change_password_enforces_policy_and_revokes_sessions():
    service = _service()
    service.bootstrap_admin("root", STRONG)
    user = service.create_user(username="frank", password=STRONG)
    token = service.authenticate("frank", STRONG).token

    with pytest.raises(DomainValidationError):
        service.change_password(user.id, STRONG, "short")
    with pytest.raises(UnauthenticatedError):
        service.change_password(user.id, "wrong-old", "Another-Strong-7!")

    service.change_password(user.id, STRONG, "Another-Strong-7!")
    assert service.validate(token) is None
    assert service.authenticate("frank", "Another-Strong-7!").user.id == user.id


def test_password_policy_categories():
    validate_password_strength("lowerUPPER123")
    validate_password_strength("lowerUPPER!symbol")
    with pytest.raises(DomainValidationError):
        validate_password_strength("onlylowercase")
    with pytest.raises(DomainValidationError):
        validate_password_strength("PasswordPassword", username="passwordpassword")


def test_remember_me_extends_session_ttl():
    from llm_d_bench.auth.security import hash_session_token

    service = _service()
    service.bootstrap_admin("root", STRONG)
    short = service.authenticate("root", STRONG)
    remembered = service.authenticate("root", STRONG, remember=True)

    short_session = service.session_dao.get_by_token_hash(hash_session_token(short.token))
    long_session = service.session_dao.get_by_token_hash(hash_session_token(remembered.token))
    short_lifetime = short_session.expires_at - short_session.created_at
    long_lifetime = long_session.expires_at - long_session.created_at
    assert long_lifetime > short_lifetime


def test_change_password_keeps_current_session():
    service = _service()
    service.bootstrap_admin("root", STRONG)
    result = service.authenticate("root", STRONG)
    service.change_password(result.user.id, STRONG, "Another-Strong-7!", keep_token=result.token)
    principal = service.validate(result.token)
    assert principal is not None
    assert principal.must_change_password is False


def test_roles_bindings_and_group_inheritance():
    service = _service()
    service.ensure_builtin_roles()
    cluster_id = ClusterDao().create("scope-cluster", "", "apiVersion: v1\nkind: Config").id

    user = service.create_user(username="gina", password=STRONG)
    end_user_role = service.require_role("end-user")
    service.add_user_binding(user.id, end_user_role.id, scope_type="cluster", scope_cluster_id=cluster_id)
    principal = service.principal_for(service.user_dao.get(user.id))
    assert reachable_clusters(principal) == {cluster_id}

    # Group grant is inherited and changes when membership changes.
    group = service.create_group(name="team-a")
    service.add_group_binding(group.id, end_user_role.id, scope_type="cluster", scope_cluster_id=cluster_id)
    service.add_group_member(group.id, user.id)
    principal = service.principal_for(service.user_dao.get(user.id))
    assert cluster_id in reachable_clusters(principal)
    assert group.id in principal.group_ids


def test_custom_role_permissions_and_builtin_protection():
    service = _service()
    service.ensure_builtin_roles()
    role = service.create_role(name="auditor", permissions={"deployment:run:read"})
    assert service.role_permission_dao.list_for_role(role.id)[0].permission == "deployment:run:read"

    with pytest.raises(DomainValidationError):
        service.create_role(name="bad", permissions={"not:a:permission"})

    admin_role = service.require_role("admin")
    with pytest.raises(DomainValidationError):
        service.set_role_permissions(admin_role.id, {"deployment:run:read"})


@pytest.mark.asyncio
async def test_directory_sync_imports_groups_and_reconciles_members(monkeypatch):
    from llm_d_bench.auth.providers.base import ExternalIdentity
    from llm_d_bench.auth.records import IdentityProviderRecord

    service = _service()
    provider = service.identity_provider_dao.create(
        IdentityProviderRecord(type="ldap", name="corp", enabled=True, config={"group_base_dn": "dc=x"})
    )

    members_by_group = {
        "cn=team,dc=x": [
            ExternalIdentity(external_id="uid=a,dc=x", username="ldap-a"),
            ExternalIdentity(external_id="uid=b,dc=x", username="ldap-b"),
        ]
    }

    class _FakeProvider:
        async def list_groups(self):
            return [{"external_id": "cn=team,dc=x", "name": "team"}]

        async def list_group_members(self, external_group):
            return list(members_by_group.get(external_group, []))

    monkeypatch.setattr("llm_d_bench.auth.service.get_provider", lambda *args, **kwargs: _FakeProvider())
    result = await service.sync_directory(provider.id)

    assert result == {"ok": True, "groups": 1, "members": 2, "failed": []}
    group = service.group_dao.get_by_external(provider.id, "cn=team,dc=x")
    assert group is not None
    assert (group.source, group.name) == ("ldap", "team")
    memberships = service.user_group_dao.list_for_group(group.id)
    assert {membership.user_id for membership in memberships} == {
        service.user_dao.get_by_username("ldap-a").id,
        service.user_dao.get_by_username("ldap-b").id,
    }
    assert all(membership.source == "external" for membership in memberships)

    # A later sync that drops a member reconciles the membership away.
    members_by_group["cn=team,dc=x"] = [ExternalIdentity(external_id="uid=a,dc=x", username="ldap-a")]
    await service.sync_directory(provider.id)
    assert {membership.user_id for membership in service.user_group_dao.list_for_group(group.id)} == {
        service.user_dao.get_by_username("ldap-a").id
    }
    assert service.identity_provider_dao.get(provider.id).last_sync_at is not None


@pytest.mark.asyncio
async def test_login_routes_a_local_user_through_the_local_password_check():
    """``login`` picks the local path for a username with ``auth_source=local`` without consulting any provider."""
    service = _service()
    service.bootstrap_admin("root", STRONG)
    service.create_user(username="alice", password=STRONG)

    result = await service.login("alice", STRONG)
    assert result.user.username == "alice"

    with pytest.raises(UnauthenticatedError):
        await service.login("alice", "wrong-password")


@pytest.mark.asyncio
async def test_login_routes_an_existing_directory_user_through_its_own_provider_only(monkeypatch):
    """A username already JIT-provisioned from a directory is verified against that one provider, never a local hash."""
    from llm_d_bench.auth.providers.base import ExternalIdentity
    from llm_d_bench.auth.records import IdentityProviderRecord, UserRecord

    service = _service()
    provider = service.identity_provider_dao.create(
        IdentityProviderRecord(type="ldap", name="corp", enabled=True, config={})
    )
    other_provider = service.identity_provider_dao.create(
        IdentityProviderRecord(type="ldap", name="other-corp", enabled=True, config={})
    )
    user = service.user_dao.create(
        UserRecord(username="sabrina", auth_source="ldap", provider_id=provider.id, external_id="uid=sabrina,dc=x")
    )
    assert user.password_hash is None

    calls: list[str] = []

    def fake_get_provider(record, *, secret=None):
        calls.append(record.id)

        class _FakeProvider:
            async def authenticate(self, username, password):
                if record.id != provider.id:
                    return None
                return ExternalIdentity(external_id="uid=sabrina,dc=x", username="sabrina") if password == "real-ldap-pw" else None

        return _FakeProvider()

    monkeypatch.setattr("llm_d_bench.auth.service.get_provider", fake_get_provider)

    result = await service.login("sabrina", "real-ldap-pw")
    assert result.user.username == "sabrina"
    # Only the user's own provider was ever consulted, not every enabled one.
    assert calls == [provider.id]
    assert other_provider.id not in calls

    with pytest.raises(UnauthenticatedError):
        await service.login("sabrina", "wrong-password")
    # A wrong password against the directory never falls back to a local check
    # and never touches the other configured provider.
    assert set(calls) == {provider.id}


@pytest.mark.asyncio
async def test_login_tries_every_enabled_provider_for_a_first_time_directory_user(monkeypatch):
    """A username with no local record yet is JIT-provisioned by whichever enabled provider recognizes it."""
    from llm_d_bench.auth.providers.base import ExternalIdentity
    from llm_d_bench.auth.records import IdentityProviderRecord

    service = _service()
    service.identity_provider_dao.create(IdentityProviderRecord(type="ldap", name="corp-a", enabled=True, config={}))
    matching = service.identity_provider_dao.create(
        IdentityProviderRecord(type="ldap", name="corp-b", enabled=True, config={})
    )

    def fake_get_provider(record, *, secret=None):
        class _FakeProvider:
            async def authenticate(self, username, password):
                if record.id != matching.id:
                    return None
                return ExternalIdentity(external_id="uid=new,dc=x", username="newcomer")

        return _FakeProvider()

    monkeypatch.setattr("llm_d_bench.auth.service.get_provider", fake_get_provider)

    result = await service.login("newcomer", "whatever")
    created = service.user_dao.get_by_username("newcomer")
    assert created is not None
    assert created.auth_source == "ldap"
    assert created.provider_id == matching.id
    assert result.user.id == created.id


def test_login_sync_materializes_directory_groups():
    from llm_d_bench.auth.providers.base import ExternalIdentity
    from llm_d_bench.auth.records import IdentityProviderRecord, UserRecord

    service = _service()
    provider = service.identity_provider_dao.create(
        IdentityProviderRecord(type="ldap", name="login-corp", enabled=True)
    )
    user = service.user_dao.create(
        UserRecord(username="ldap-login", auth_source="ldap", provider_id=provider.id, external_id="uid=l,dc=x")
    )

    identity = ExternalIdentity(external_id="uid=l,dc=x", username="ldap-login", groups=("cn=team,dc=x",))
    service._sync_external_groups(provider, user, identity)

    group = service.group_dao.get_by_external(provider.id, "cn=team,dc=x")
    assert group is not None
    assert (group.source, group.name) == ("ldap", "team")
    assert [membership.group_id for membership in service.user_group_dao.list_for_user(user.id)] == [group.id]

    # The directory no longer reports the group: the external membership is removed.
    service._sync_external_groups(
        provider, user, ExternalIdentity(external_id="uid=l,dc=x", username="ldap-login", groups=())
    )
    assert service.user_group_dao.list_for_user(user.id) == []


def test_directory_group_membership_is_read_only_in_lens():
    from llm_d_bench.auth.records import GroupRecord, IdentityProviderRecord

    service = _service()
    provider = service.identity_provider_dao.create(IdentityProviderRecord(type="ldap", name="corp-mem", enabled=True))
    group = service.group_dao.create(
        GroupRecord(name="dir-group", source="ldap", provider_id=provider.id, external_id="cn=g,dc=x")
    )

    with pytest.raises(DomainValidationError):
        service.add_group_member(group.id, "someone")
    with pytest.raises(DomainValidationError):
        service.remove_group_member(group.id, "someone")


def test_delete_identity_provider_removes_external_identities_and_authorizations():
    from llm_d_bench.auth.records import (
        GroupRecord,
        GroupRoleBindingRecord,
        IdentityProviderRecord,
        UserGroupRecord,
        UserRecord,
        UserRoleBindingRecord,
    )

    service = _service()
    service.ensure_builtin_roles()
    provider = service.identity_provider_dao.create(IdentityProviderRecord(type="ldap", name="corp-del", enabled=True))
    user = service.user_dao.create(
        UserRecord(username="ldap-del", auth_source="ldap", provider_id=provider.id, external_id="uid=del,dc=x")
    )
    group = service.group_dao.create(
        GroupRecord(name="ldap-del-group", source="ldap", provider_id=provider.id, external_id="cn=del,dc=x")
    )
    maintainer = service.require_role("maintainer")
    service.user_binding_dao.create(
        UserRoleBindingRecord(user_id=user.id, role_id=maintainer.id, scope_type="global")
    )
    service.group_binding_dao.create(
        GroupRoleBindingRecord(group_id=group.id, role_id=maintainer.id, scope_type="global")
    )
    service.user_group_dao.add(UserGroupRecord(user_id=user.id, group_id=group.id, source="external"))

    service.delete_identity_provider(provider.id)

    assert service.identity_provider_dao.get(provider.id) is None
    assert service.user_dao.get(user.id) is None
    assert service.group_dao.get(group.id) is None
    assert service.user_binding_dao.list_for_user(user.id) == []
    assert service.group_binding_dao.list_for_group(group.id) == []


def test_delete_identity_provider_refuses_to_remove_the_last_admin():
    from llm_d_bench.auth.records import IdentityProviderRecord, UserRecord, UserRoleBindingRecord

    service = _service()
    service.ensure_builtin_roles()
    provider = service.identity_provider_dao.create(
        IdentityProviderRecord(type="ldap", name="corp-admin", enabled=True)
    )
    user = service.user_dao.create(
        UserRecord(username="ldap-admin", auth_source="ldap", provider_id=provider.id, external_id="uid=admin,dc=x")
    )
    admin_role = service.require_role("admin")
    service.user_binding_dao.create(UserRoleBindingRecord(user_id=user.id, role_id=admin_role.id, scope_type="global"))

    with pytest.raises(ConflictError):
        service.delete_identity_provider(provider.id)


def test_protected_admin_cannot_be_deleted_or_reroled():
    from llm_d_bench.auth.records import UserRoleBindingRecord

    service = _service()
    service.ensure_builtin_roles()
    admin = service.create_user(username="admin", password=STRONG)
    admin_role = service.require_role("admin")
    service.user_binding_dao.create(UserRoleBindingRecord(user_id=admin.id, role_id=admin_role.id, scope_type="global"))

    with pytest.raises(ConflictError):
        service.delete_user(admin.id)
    with pytest.raises(ConflictError):
        service.add_user_binding(admin.id, service.require_role("end-user").id)
    binding = service.user_binding_dao.list_for_user(admin.id)[0]
    with pytest.raises(ConflictError):
        service.remove_user_binding(admin.id, binding.id)
    with pytest.raises(ConflictError):
        service.update_user(admin.id, status="disabled")
    # Password changes remain allowed.
    service.reset_password(admin.id, "Another-Strong-7!")


def test_user_cannot_delete_self():
    service = _service()
    admin = service.bootstrap_admin("root", STRONG)
    principal = service.principal_for(admin)
    with pytest.raises(DomainValidationError):
        service.delete_user(admin.id, actor=principal)


def test_last_admin_cannot_be_deleted():
    service = _service()
    admin = service.bootstrap_admin("root", STRONG)
    with pytest.raises(ConflictError):
        service.delete_user(admin.id)
    second = service.create_user(username="root2", password=STRONG)
    admin_role = service.require_role("admin")
    service.add_user_binding(second.id, admin_role.id, scope_type="global")
    # With two admins, removing one is allowed.
    service.delete_user(second.id)


def test_revoke_session_records_audit_actor():
    service = _service()
    service.bootstrap_admin("root", STRONG)
    user = service.create_user(username="frank", password=STRONG)
    login = service.authenticate("frank", STRONG)
    session = service.session_dao.get_by_token_hash(hash_session_token(login.token))
    assert session is not None

    principal = service.principal_for(service.user_dao.get(user.id))
    service.revoke_session(session.id, actor=principal)

    events = service.audit_dao.list(event_type="session_revoked", limit=10)
    assert len(events) == 1
    assert events[0].actor_user_id == user.id
    assert events[0].actor_username == "frank"
    assert events[0].target_type == "session"
    assert events[0].target_id == session.id
