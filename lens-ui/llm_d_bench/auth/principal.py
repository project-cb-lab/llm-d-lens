"""Assemble a policy ``Principal`` from persisted users, groups and bindings.

This is the seam between the DAO layer and the pure policy core: it resolves
role ids to names, folds group-inherited bindings into one effective set, and
carries the version used for cache invalidation (design sections 7.2/7.12).
"""

from __future__ import annotations

from llm_d_bench.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_bench.auth.permissions import BUILTIN_ROLE_PERMISSIONS, role_self_scoped
from llm_d_bench.auth.records import UserRecord
from llm_d_bench.db.dao.group import GroupDao, GroupRoleBindingDao, UserGroupDao
from llm_d_bench.db.dao.role import RoleDao, RolePermissionDao, UserRoleBindingDao


def _to_binding(record, role_name: str) -> RoleBinding:
    return RoleBinding(
        role=role_name,
        scope_type=ScopeType(record.scope_type),
        scope_cluster_id=record.scope_cluster_id,
        scope_resource_type=record.scope_resource_type,
        scope_resource_id=record.scope_resource_id,
    )


def build_principal(
    user: UserRecord,
    *,
    user_group_dao: UserGroupDao | None = None,
    group_dao: GroupDao | None = None,
    user_binding_dao: UserRoleBindingDao | None = None,
    group_binding_dao: GroupRoleBindingDao | None = None,
    role_dao: RoleDao | None = None,
    role_permission_dao: RolePermissionDao | None = None,
) -> Principal:
    """Build the effective principal for ``user`` (own + group-inherited grants)."""
    user_group_dao = user_group_dao or UserGroupDao()
    group_dao = group_dao or GroupDao()
    user_binding_dao = user_binding_dao or UserRoleBindingDao()
    group_binding_dao = group_binding_dao or GroupRoleBindingDao()
    role_dao = role_dao or RoleDao()
    role_permission_dao = role_permission_dao or RolePermissionDao()

    memberships = user_group_dao.list_for_user(user.id)
    group_ids = frozenset(membership.group_id for membership in memberships)

    raw_bindings = list(user_binding_dao.list_for_user(user.id))
    authz_versions = [user.principal_version]
    for group_id in group_ids:
        raw_bindings.extend(group_binding_dao.list_for_group(group_id))
        group = group_dao.get(group_id)
        if group is not None:
            authz_versions.append(group.authz_version)

    role_names: dict[str, str] = {}
    for binding in raw_bindings:
        if binding.role_id not in role_names:
            role = role_dao.get(binding.role_id)
            role_names[binding.role_id] = role.name if role is not None else binding.role_id

    role_ids = {binding.role_id for binding in raw_bindings}
    permissions_by_role_id = role_permission_dao.permissions_for_roles(role_ids)

    bindings = tuple(_to_binding(binding, role_names[binding.role_id]) for binding in raw_bindings)
    role_permissions: dict[str, frozenset[str]] = {}
    for role_id, name in role_names.items():
        codes = permissions_by_role_id.get(role_id)
        if not codes and name in BUILTIN_ROLE_PERMISSIONS:
            # Fall back to the catalog when a built-in role has not been seeded
            # into role_permissions yet (e.g. a fresh database).
            codes = BUILTIN_ROLE_PERMISSIONS[name]
        role_permissions[name] = frozenset(codes or ())
    role_scopes = {name: role_self_scoped(name) for name in set(role_names.values())}

    return Principal(
        user_id=user.id,
        username=user.username,
        bindings=bindings,
        role_permissions=role_permissions,
        role_self_scoped=role_scopes,
        group_ids=group_ids,
        authz_version=max(authz_versions) if authz_versions else user.principal_version,
        must_change_password=bool(user.must_change_password),
    )
