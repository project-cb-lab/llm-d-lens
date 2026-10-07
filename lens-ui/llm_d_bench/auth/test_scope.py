from llm_d_bench.auth.contracts import Principal, ResourceRef, RoleBinding, ScopeType
from llm_d_bench.auth.scope import (
    binding_applies,
    cluster_reachable,
    is_creator,
    is_owner_group_member,
    ownership_covers,
    reachable_clusters,
)


def _principal(*bindings, groups=frozenset(), user_id="u1"):
    return Principal(user_id=user_id, username=user_id, bindings=tuple(bindings), group_ids=groups)


def _resource(cluster="c1", owner="u1", owner_group=None):
    return ResourceRef(
        resource_type="deployment_run",
        resource_id="r1",
        cluster_id=cluster,
        owner_user_id=owner,
        owner_group_id=owner_group,
    )


def test_global_binding_means_unrestricted_clusters():
    principal = _principal(RoleBinding("admin", ScopeType.GLOBAL))
    assert reachable_clusters(principal) is None
    assert cluster_reachable(principal, "any")


def test_cluster_bindings_union():
    principal = _principal(
        RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="a"),
        RoleBinding("end-user", ScopeType.CLUSTER, scope_cluster_id="b"),
    )
    assert reachable_clusters(principal) == {"a", "b"}
    assert cluster_reachable(principal, "b")
    assert not cluster_reachable(principal, "c")


def test_resource_binding_alone_does_not_grant_cluster_reachability():
    binding = RoleBinding(
        "end-user",
        ScopeType.RESOURCE,
        scope_cluster_id="a",
        scope_resource_type="deployment_run",
        scope_resource_id="r1",
    )
    assert reachable_clusters(_principal(binding)) == set()
    assert not cluster_reachable(_principal(binding), "a")


def test_binding_applies_matches_scope():
    resource = _resource(cluster="a")
    assert binding_applies(RoleBinding("admin", ScopeType.GLOBAL), resource)
    assert binding_applies(RoleBinding("m", ScopeType.CLUSTER, scope_cluster_id="a"), resource)
    assert not binding_applies(RoleBinding("m", ScopeType.CLUSTER, scope_cluster_id="b"), resource)
    shared = RoleBinding(
        "end-user",
        ScopeType.RESOURCE,
        scope_cluster_id="a",
        scope_resource_type="deployment_run",
        scope_resource_id="r1",
    )
    assert binding_applies(shared, resource)
    assert not binding_applies(
        RoleBinding(
            "end-user",
            ScopeType.RESOURCE,
            scope_cluster_id="a",
            scope_resource_type="deployment_run",
            scope_resource_id="other",
        ),
        resource,
    )


def test_creator_identity():
    resource = _resource(owner="u1")
    assert is_creator(_principal(user_id="u1"), resource)
    assert not is_creator(_principal(user_id="u2"), resource)
    assert not is_creator(_principal(user_id="u2"), _resource(owner=None))


def test_owner_group_membership():
    resource = _resource(owner="someone", owner_group="g1")
    assert is_owner_group_member(_principal(groups=frozenset({"g1"})), resource)
    assert not is_owner_group_member(_principal(groups=frozenset({"g2"})), resource)


def test_ownership_covers_operate_but_not_delete_for_group_member():
    resource = _resource(owner="creator", owner_group="g1")
    member = _principal(
        RoleBinding("end-user", ScopeType.CLUSTER, scope_cluster_id="c1"),
        groups=frozenset({"g1"}),
        user_id="member",
    )
    assert ownership_covers(member, resource, "deployment:run:read")
    assert ownership_covers(member, resource, "monitoring:deployment:action")
    assert not ownership_covers(member, resource, "deployment:execution:delete")
    assert not ownership_covers(member, resource, "deployment:run:share")


def test_ownership_covers_all_actions_for_creator():
    resource = _resource(owner="creator")
    creator = _principal(RoleBinding("end-user", ScopeType.CLUSTER, scope_cluster_id="c1"), user_id="creator")
    assert ownership_covers(creator, resource, "deployment:execution:delete")
    assert ownership_covers(creator, resource, "deployment:run:share")


def test_ownership_requires_cluster_reachability():
    resource = _resource(cluster="other", owner="creator")
    creator = _principal(RoleBinding("end-user", ScopeType.CLUSTER, scope_cluster_id="c1"), user_id="creator")
    assert not ownership_covers(creator, resource, "deployment:run:read")
