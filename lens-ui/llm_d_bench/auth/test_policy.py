from llm_d_bench.auth.access import owner_for_create, resource_readable
from llm_d_bench.auth.contracts import Principal, ResourceRef, RoleBinding, ScopeType
from llm_d_bench.auth.permissions import BUILTIN_ROLE_PERMISSIONS, BUILTIN_ROLE_SELF_SCOPED
from llm_d_bench.auth.policy import accessible_cluster_ids, authorize, role_has_permission

#: Synthetic self-scoped workload role for policy-engine tests. The built-in
#: ``end-user`` is model-service-only (D15), so ownership/self-scope semantics
#: are exercised through this role instead.
_WORKLOAD_PERMISSIONS = frozenset(
    {
        "deployment:run:read",
        "deployment:run:cancel",
        "deployment:run:share",
        "deployment:execution:update",
        "deployment:execution:delete",
        "deployment:execution:connect",
        "deployment:case:execute",
        "deployment:session:rebind",
        "deployment:agentic:read",
        "evaluate:run:read",
        "evaluate:run:cancel",
        "evaluate:run:retry",
        "evaluate:run:delete",
        "simulation:task:read",
        "simulation:task:stop",
        "simulation:task:rerun",
        "simulation:task:delete",
        "configuration:artifact:read",
        "configuration:artifact:delete",
        "configuration:artifact:render",
        "monitoring:deployment:read",
        "monitoring:deployment:action",
    }
)

_TEST_ROLE_PERMISSIONS = {**BUILTIN_ROLE_PERMISSIONS, "workload": _WORKLOAD_PERMISSIONS}
_TEST_ROLE_SELF_SCOPED = {**BUILTIN_ROLE_SELF_SCOPED, "workload": _WORKLOAD_PERMISSIONS}


def _principal(*bindings, groups=frozenset(), user_id="u1"):
    return Principal(
        user_id=user_id,
        username=user_id,
        bindings=tuple(bindings),
        role_permissions=_TEST_ROLE_PERMISSIONS,
        role_self_scoped=_TEST_ROLE_SELF_SCOPED,
        group_ids=groups,
    )


def _deployment(cluster="c1", owner="u1", owner_group=None):
    return ResourceRef(
        resource_type="deployment_run",
        resource_id="r1",
        cluster_id=cluster,
        owner_user_id=owner,
        owner_group_id=owner_group,
    )


def _end_user(cluster="c1", **kwargs):
    return _principal(RoleBinding("workload", ScopeType.CLUSTER, scope_cluster_id=cluster), **kwargs)


def test_global_admin_is_allowed_everything():
    admin = _principal(RoleBinding("admin", ScopeType.GLOBAL))
    assert authorize(admin, "cluster:cluster:delete", resource=ResourceRef("cluster", "c", "c1")).allowed
    assert authorize(admin, "system:database:configure").allowed
    assert accessible_cluster_ids(admin) is None


def _maintainer(cluster="c1", **kwargs):
    return _principal(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id=cluster), **kwargs)


def test_create_requires_binding_on_target_cluster():
    user = _maintainer(cluster="c1")
    assert authorize(user, "deployment:run:create", target_cluster_id="c1").allowed
    assert not authorize(user, "deployment:run:create", target_cluster_id="c2").allowed


def test_end_user_cannot_create_deployment():
    decision = authorize(_end_user(), "deployment:run:create", target_cluster_id="c1")
    assert not decision.allowed
    assert decision.reason == "action_not_granted"


def test_end_user_cannot_create_cluster_even_from_global_scope():
    # end-user role lacks cluster:cluster:create; a global binding does not add actions.
    user = _principal(RoleBinding("end-user", ScopeType.GLOBAL))
    decision = authorize(user, "cluster:cluster:create", target_cluster_id="c1")
    assert not decision.allowed
    assert decision.reason == "action_not_granted"


def test_creator_can_delete_own_deployment():
    decision = authorize(_end_user(), "deployment:execution:delete", resource=_deployment(owner="u1"))
    assert decision.allowed
    assert decision.reason == "ownership"


def test_non_owner_cannot_touch_foreign_deployment():
    other = _end_user(user_id="u2")
    resource = _deployment(owner="u1")
    read = authorize(other, "deployment:run:read", resource=resource)
    assert not read.allowed
    assert read.reason == "out_of_scope"
    # The end-user role contains deployment:execution:delete, but a cluster
    # binding must not grant it on someone else's deployment.
    assert not authorize(other, "deployment:execution:delete", resource=resource).allowed
    assert not authorize(other, "monitoring:deployment:action", resource=resource).allowed


def test_owner_group_member_can_operate_but_not_delete():
    member = _end_user(groups=frozenset({"g1"}), user_id="u3")
    resource = _deployment(owner="creator", owner_group="g1")
    assert authorize(member, "deployment:run:read", resource=resource).allowed
    assert authorize(member, "monitoring:deployment:action", resource=resource).allowed
    assert not authorize(member, "deployment:execution:delete", resource=resource).allowed


def test_resource_share_requires_existing_cluster_access():
    shared = RoleBinding(
        "workload",
        ScopeType.RESOURCE,
        scope_cluster_id="c1",
        scope_resource_type="deployment_run",
        scope_resource_id="r1",
    )
    resource = _deployment(cluster="c1", owner="creator")
    with_cluster = _principal(
        RoleBinding("workload", ScopeType.CLUSTER, scope_cluster_id="c1"),
        shared,
        user_id="u4",
    )
    assert authorize(with_cluster, "deployment:run:read", resource=resource).allowed
    # Without a cluster/global binding, the share alone grants nothing.
    share_only = _principal(shared, user_id="u5")
    assert not authorize(share_only, "deployment:run:read", resource=resource).allowed


def test_share_does_not_cross_cluster():
    shared = RoleBinding(
        "workload",
        ScopeType.RESOURCE,
        scope_cluster_id="c1",
        scope_resource_type="deployment_run",
        scope_resource_id="r1",
    )
    user = _principal(
        RoleBinding("workload", ScopeType.CLUSTER, scope_cluster_id="c1"),
        shared,
        user_id="u6",
    )
    assert not authorize(user, "deployment:run:read", resource=_deployment(cluster="c2", owner="creator")).allowed


def test_list_action_gate_only():
    user = _end_user()
    assert authorize(user, "deployment:run:read").allowed
    assert not authorize(user, "cluster:cluster:delete").allowed


def _deployment_readable(user, *, owner, owner_group=None, cluster="c1", user_id=None):
    return resource_readable(
        user,
        permission="deployment:run:read",
        resource_type="deployment_run",
        resource_id="r1",
        cluster_id=cluster,
        owner_user_id=owner,
        owner_group_id=owner_group,
    )


def test_resource_readable_honors_ownership_for_end_user():
    creator = _end_user(user_id="owner")
    other = _end_user(user_id="other")
    assert _deployment_readable(creator, owner="owner") is True
    assert _deployment_readable(other, owner="owner") is False
    assert _deployment_readable(other, owner=None) is False
    assert _deployment_readable(other, owner="other") is True


def test_resource_readable_allows_maintainer_scope():
    maintainer = _principal(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="c1"), user_id="m")
    assert _deployment_readable(maintainer, owner="someone-else") is True


def test_resource_readable_allows_owner_group_and_none_principal():
    member = _end_user(groups=frozenset({"g1"}), user_id="member")
    assert _deployment_readable(member, owner="creator", owner_group="g1") is True
    # No principal (direct handler call / auth disabled) is unrestricted.
    assert (
        resource_readable(
            None,
            permission="deployment:run:read",
            resource_type="deployment_run",
            resource_id="r1",
            cluster_id="c1",
            owner_user_id="someone",
        )
        is True
    )


def test_owner_for_create_ignores_disabled_sentinel():
    assert owner_for_create(None) is None
    assert owner_for_create(_principal(user_id="system")) is None
    assert owner_for_create(_principal(user_id="alice")) == "alice"


def test_casbin_global_binding_applies_across_clusters():
    maintainer = _principal(RoleBinding("maintainer", ScopeType.GLOBAL), user_id="m")
    resource = _deployment(cluster="c9", owner="someone")
    assert authorize(maintainer, "deployment:execution:delete", resource=resource).allowed


def test_casbin_cluster_binding_is_scoped_to_its_cluster():
    maintainer = _principal(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="c1"), user_id="m")
    allowed = authorize(maintainer, "deployment:execution:delete", resource=_deployment(cluster="c1", owner="x"))
    denied = authorize(maintainer, "deployment:execution:delete", resource=_deployment(cluster="c2", owner="x"))
    assert allowed.allowed
    assert not denied.allowed


def test_casbin_wildcard_permission_in_custom_role():
    custom = Principal(
        user_id="u9",
        username="u9",
        bindings=(RoleBinding("reviewer", ScopeType.CLUSTER, scope_cluster_id="c1"),),
        role_permissions={"reviewer": frozenset({"deployment:*:*"})},
        role_self_scoped={},
    )
    assert authorize(custom, "deployment:run:read", resource=_deployment(cluster="c1", owner="x")).allowed
    assert authorize(custom, "deployment:execution:delete", resource=_deployment(cluster="c1", owner="x")).allowed


def test_role_has_permission_is_scope_sensitive():
    user = _maintainer(cluster="c1")
    assert role_has_permission(user, "deployment:run:create", target_cluster_id="c1")
    assert not role_has_permission(user, "deployment:run:create", target_cluster_id="c2")


def _resource_share(role="workload", cluster="c1", resource_type="deployment_run", resource_id="r1"):
    return RoleBinding(
        role,
        ScopeType.RESOURCE,
        scope_cluster_id=cluster,
        scope_resource_type=resource_type,
        scope_resource_id=resource_id,
    )


def test_resource_share_confers_operate_but_not_creator_only_actions():
    grantee = _principal(
        RoleBinding("workload", ScopeType.CLUSTER, scope_cluster_id="c1"),
        _resource_share(),
        user_id="u9",
    )
    resource = _deployment(cluster="c1", owner="creator")
    assert authorize(grantee, "deployment:run:read", resource=resource).allowed
    assert authorize(grantee, "deployment:execution:update", resource=resource).allowed
    assert authorize(grantee, "deployment:execution:connect", resource=resource).allowed
    # delete and re-share remain creator-only even though the shared end-user
    # role lists them; the grantee is not the creator.
    assert not authorize(grantee, "deployment:execution:delete", resource=resource).allowed
    assert not authorize(grantee, "deployment:run:share", resource=resource).allowed


def test_maintainer_share_can_confer_delete():
    grantee = _principal(
        RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="c1"),
        _resource_share(role="maintainer"),
        user_id="u10",
    )
    resource = _deployment(cluster="c1", owner="creator")
    assert authorize(grantee, "deployment:execution:delete", resource=resource).allowed
