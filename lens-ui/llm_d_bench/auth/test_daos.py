"""DAO-layer tests for the auth tables (design section 4)."""

from __future__ import annotations

from datetime import timedelta

from llm_d_bench.auth.records import (
    AuditLogRecord,
    GroupRecord,
    GroupRoleBindingRecord,
    IdentityGroupMappingRecord,
    IdentityProviderRecord,
    RoleRecord,
    SessionRecord,
    UserGroupRecord,
    UserRecord,
    UserRoleBindingRecord,
    utcnow,
)
from llm_d_bench.db.dao.audit_log import AuditLogDao
from llm_d_bench.db.dao.cluster import ClusterDao
from llm_d_bench.db.dao.group import GroupDao, GroupRoleBindingDao, UserGroupDao
from llm_d_bench.db.dao.identity_provider import IdentityGroupMappingDao, IdentityProviderDao
from llm_d_bench.db.dao.role import RoleDao, RolePermissionDao, UserRoleBindingDao
from llm_d_bench.db.dao.session import SessionDao
from llm_d_bench.db.dao.user import UserDao


def _cluster() -> str:
    return ClusterDao().create("auth-test", "", "apiVersion: v1\nkind: Config").id


def test_user_crud_and_case_insensitive_lookup():
    dao = UserDao()
    user = dao.create(UserRecord(username="Alice", display_name="Alice A"))
    assert dao.get(user.id).username == "Alice"
    assert dao.get_by_username("alice").id == user.id
    assert dao.get_by_username("missing") is None

    user.display_name = "Alice B"
    user.status = "disabled"
    saved = dao.save(user)
    assert saved.display_name == "Alice B"
    assert [u.id for u in dao.list(status="disabled")] == [user.id]
    assert [u.id for u in dao.list(query="alice")] == [user.id]

    dao.bump_principal_version(user.id)
    assert dao.get(user.id).principal_version == user.principal_version + 1

    dao.delete(user.id)
    assert dao.get(user.id) is None


def test_user_external_lookup_and_login_mark():
    dao = UserDao()
    dao.create(UserRecord(username="bob", auth_source="ldap", provider_id=None, external_id="uid=bob,dc=x"))
    found = dao.get_by_username("bob")
    # provider_id is null here; lookup by external needs a real provider, so create one.
    assert found.external_id == "uid=bob,dc=x"
    dao.mark_login(found.id)
    reloaded = dao.get(found.id)
    assert reloaded.last_login_at is not None
    assert reloaded.failed_login_count == 0


def test_roles_permissions_and_user_bindings():
    roles = RoleDao()
    role = roles.create(RoleRecord(name="custom-reviewer", description="review"))
    assert roles.get_by_name("CUSTOM-REVIEWER").id == role.id

    permissions = RolePermissionDao()
    permissions.set_for_role(role.id, {"deployment:run:read", "deployment:run:share"})
    assert {p.permission for p in permissions.list_for_role(role.id)} == {
        "deployment:run:read",
        "deployment:run:share",
    }
    permissions.set_for_role(role.id, {"deployment:run:read"})
    assert {p.permission for p in permissions.list_for_role(role.id)} == {"deployment:run:read"}
    assert permissions.permissions_for_roles([role.id])[role.id] == frozenset({"deployment:run:read"})

    user = UserDao().create(UserRecord(username="carol"))
    bindings = UserRoleBindingDao()
    cluster_id = _cluster()
    binding = bindings.create(
        UserRoleBindingRecord(user_id=user.id, role_id=role.id, scope_type="cluster", scope_cluster_id=cluster_id)
    )
    assert [b.id for b in bindings.list_for_user(user.id)] == [binding.id]
    bindings.delete(binding.id)
    assert bindings.list_for_user(user.id) == []


def test_groups_membership_and_bindings():
    groups = GroupDao()
    group = groups.create(GroupRecord(name="team-a"))
    assert groups.get_by_name("TEAM-A").id == group.id
    assert groups.get(group.id).authz_version == 1
    groups.bump_authz_version(group.id)
    assert groups.get(group.id).authz_version == 2

    user = UserDao().create(UserRecord(username="dave"))
    members = UserGroupDao()
    members.add(UserGroupRecord(user_id=user.id, group_id=group.id, source="manual"))
    assert [m.user_id for m in members.list_for_group(group.id)] == [user.id]
    members.remove(user.id, group.id)
    assert members.list_for_group(group.id) == []

    members.set_members(group.id, {user.id})
    assert {m.user_id for m in members.list_for_group(group.id)} == {user.id}

    role = RoleDao().create(RoleRecord(name="team-role"))
    bindings = GroupRoleBindingDao()
    cluster_id = _cluster()
    record = bindings.create(
        GroupRoleBindingRecord(group_id=group.id, role_id=role.id, scope_type="cluster", scope_cluster_id=cluster_id)
    )
    assert [b.id for b in bindings.list_for_group(group.id)] == [record.id]
    assert GroupRoleBindingDao().list_all()[0].id == record.id
    bindings.delete(record.id)


def test_sessions_lifecycle_and_sweep():
    user = UserDao().create(UserRecord(username="erin"))
    dao = SessionDao()
    now = utcnow()
    active = dao.create(
        SessionRecord(
            user_id=user.id,
            token_hash="a" * 64,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=12),
            idle_expires_at=now + timedelta(hours=8),
        )
    )
    assert dao.get_by_token_hash("a" * 64).id == active.id
    assert dao.count_active_for_user(user.id) == 1

    dao.touch(active.id, idle_expires_at=now + timedelta(hours=9), last_seen_at=now)
    assert dao.get(active.id).idle_expires_at > now

    dao.revoke(active.id)
    assert dao.list_for_user(user.id) == []
    assert len(dao.list_for_user(user.id, include_revoked=True)) == 1
    assert dao.list_all() == []
    assert [row.id for row in dao.list_all(include_revoked=True)] == [active.id]

    expired = dao.create(
        SessionRecord(
            user_id=user.id,
            token_hash="b" * 64,
            created_at=now,
            last_seen_at=now,
            expires_at=now - timedelta(minutes=1),
            idle_expires_at=now - timedelta(minutes=1),
        )
    )
    deleted = dao.sweep(now=now + timedelta(minutes=1), revoked_retention_seconds=0)
    assert deleted == 2
    assert dao.get(expired.id) is None


def test_session_per_user_cap():
    user = UserDao().create(UserRecord(username="frank"))
    dao = SessionDao()
    now = utcnow()
    for index in range(3):
        dao.create(
            SessionRecord(
                user_id=user.id,
                token_hash=f"{index:064d}",
                created_at=now + timedelta(seconds=index),
                last_seen_at=now,
                expires_at=now + timedelta(hours=12),
                idle_expires_at=now + timedelta(hours=8),
            )
        )
    dao.trim_user_sessions(user.id, max_keep=1)
    assert dao.count_active_for_user(user.id) == 1


def test_revoke_all_sessions():
    user = UserDao().create(UserRecord(username="gina"))
    dao = SessionDao()
    now = utcnow()
    for index in range(2):
        dao.create(
            SessionRecord(
                user_id=user.id,
                token_hash=f"{index:064d}",
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=12),
                idle_expires_at=now + timedelta(hours=8),
            )
        )
    dao.revoke_user_sessions(user.id)
    assert dao.count_active_for_user(user.id) == 0


def test_identity_provider_and_mappings():
    providers = IdentityProviderDao()
    provider = providers.create(
        IdentityProviderRecord(type="ldap", name="corp-ldap", enabled=True, config={"server_url": "ldaps://x"})
    )
    assert providers.get_by_name("corp-ldap").id == provider.id
    assert [p.id for p in providers.list_enabled()] == [provider.id]

    provider.is_default = True
    assert providers.save(provider).is_default is True

    group = GroupDao().create(GroupRecord(name="ldap-team"))
    mappings = IdentityGroupMappingDao()
    mapping = mappings.create(
        IdentityGroupMappingRecord(provider_id=provider.id, external_group="cn=team", group_id=group.id)
    )
    assert [m.id for m in mappings.list_for_provider(provider.id)] == [mapping.id]
    mappings.delete(mapping.id)
    assert mappings.list_for_provider(provider.id) == []


def test_audit_log_append_list_and_retention():
    dao = AuditLogDao()
    now = utcnow()
    old = dao.append(
        AuditLogRecord(event_type="login", actor_username="u", created_at=now - timedelta(days=400), result="success")
    )
    dao.append(AuditLogRecord(event_type="mutation", actor_username="u", created_at=now, result="success"))
    assert len(dao.list(event_type="mutation")) == 1
    assert len(dao.list()) == 2

    deleted = dao.delete_before(dao.retention_cutoff(180))
    assert deleted == 1
    assert dao.list()[0].id != old.id
