"""Resource-share service tests (design section 7.7)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from llm_d_bench.auth.contracts import ResourceRef
from llm_d_bench.auth.policy import authorize
from llm_d_bench.auth.records import GroupRecord, RoleRecord, UserRecord, UserRoleBindingRecord
from llm_d_bench.auth.service import default_service
from llm_d_bench.core.exceptions import ConflictError, DomainValidationError, ForbiddenError
from llm_d_bench.db.dao.cluster import ClusterDao
from llm_d_bench.db.dao.group import GroupDao
from llm_d_bench.db.dao.role import RoleDao, RolePermissionDao, UserRoleBindingDao
from llm_d_bench.db.dao.user import UserDao


def _cluster() -> str:
    return ClusterDao().create(f"share-{uuid4().hex[:6]}", "", "apiVersion: v1\nkind: Config").id


def _role(permissions: set[str]) -> str:
    role = RoleDao().create(RoleRecord(name=f"share-role-{uuid4().hex[:8]}", description=""))
    RolePermissionDao().set_for_role(role.id, permissions)
    return role.id


def _reachable_user(role_id: str, cluster_id: str, username: str) -> str:
    user = UserDao().create(UserRecord(username=username))
    UserRoleBindingDao().create(
        UserRoleBindingRecord(user_id=user.id, role_id=role_id, scope_type="cluster", scope_cluster_id=cluster_id)
    )
    return user.id


def test_share_requires_a_reachable_cluster_for_the_grantee():
    service = default_service()
    cluster_id = _cluster()
    role_id = _role({"deployment:run:read", "deployment:run:share"})
    grantee = UserDao().create(UserRecord(username=f"grantee-{uuid4().hex[:6]}")).id
    with pytest.raises(ForbiddenError):
        service.share_resource(
            subject_type="user",
            subject_id=grantee,
            role_id=role_id,
            resource_type="deployment_run",
            resource_id="run-1",
            cluster_id=cluster_id,
        )


def test_share_grants_read_scope_and_lists_and_revokes():
    service = default_service()
    cluster_id = _cluster()
    # Reachability comes from a cluster binding; the share itself adds the read
    # action scoped to one execution (a cluster role without it grants nothing).
    base_role_id = _role({"cluster:cluster:read"})
    share_role_id = _role({"deployment:run:read"})
    grantee = _reachable_user(base_role_id, cluster_id, f"reader-{uuid4().hex[:6]}")

    binding = service.share_resource(
        subject_type="user",
        subject_id=grantee,
        role_id=share_role_id,
        resource_type="deployment_execution",
        resource_id="exec-1",
        cluster_id=cluster_id,
    )
    listed = service.list_resource_shares("deployment_execution", "exec-1")
    assert [entry[1].id for entry in listed] == [binding.id]

    principal = service.principal_for(UserDao().get(grantee))
    resource = ResourceRef(resource_type="deployment_execution", resource_id="exec-1", cluster_id=cluster_id)
    assert authorize(principal, "deployment:run:read", resource=resource).allowed

    # A different execution in the same cluster is not covered by the share.
    other = ResourceRef(resource_type="deployment_execution", resource_id="exec-2", cluster_id=cluster_id)
    assert not authorize(principal, "deployment:run:read", resource=other).allowed

    service.revoke_resource_share(
        binding.id, resource_type="deployment_execution", resource_id="exec-1", cluster_id=cluster_id
    )
    assert service.list_resource_shares("deployment_execution", "exec-1") == []


def test_duplicate_share_is_rejected():
    service = default_service()
    cluster_id = _cluster()
    role_id = _role({"deployment:run:read"})
    grantee = _reachable_user(role_id, cluster_id, f"dupe-{uuid4().hex[:6]}")
    kwargs = {
        "subject_type": "user",
        "subject_id": grantee,
        "role_id": role_id,
        "resource_type": "deployment_run",
        "resource_id": "run-dupe",
        "cluster_id": cluster_id,
    }
    service.share_resource(**kwargs)
    with pytest.raises(ConflictError):
        service.share_resource(**kwargs)


def test_unsupported_resource_type_is_rejected():
    service = default_service()
    cluster_id = _cluster()
    role_id = _role({"deployment:run:read"})
    grantee = _reachable_user(role_id, cluster_id, f"type-{uuid4().hex[:6]}")
    with pytest.raises(DomainValidationError):
        service.share_resource(
            subject_type="user",
            subject_id=grantee,
            role_id=role_id,
            resource_type="cluster",
            resource_id="c1",
            cluster_id=cluster_id,
        )


def test_group_share_requires_a_group_cluster_binding():
    service = default_service()
    cluster_id = _cluster()
    role_id = _role({"deployment:run:read"})
    group = GroupDao().create(GroupRecord(name=f"share-group-{uuid4().hex[:6]}"))
    with pytest.raises(ForbiddenError):
        service.share_resource(
            subject_type="group",
            subject_id=group.id,
            role_id=role_id,
            resource_type="deployment_run",
            resource_id="run-group",
            cluster_id=cluster_id,
        )


def test_revoke_resource_shares_removes_user_and_group_bindings_only_for_that_resource():
    service = default_service()
    cluster_id = _cluster()
    role_id = _role({"deployment:run:read"})
    grantee = _reachable_user(role_id, cluster_id, f"bulk-{uuid4().hex[:6]}")
    group = GroupDao().create(GroupRecord(name=f"bulk-group-{uuid4().hex[:6]}"))
    service.add_group_binding(group.id, role_id, scope_type="cluster", scope_cluster_id=cluster_id)

    for subject_type, subject_id in (("user", grantee), ("group", group.id)):
        service.share_resource(
            subject_type=subject_type,
            subject_id=subject_id,
            role_id=role_id,
            resource_type="deployment_execution",
            resource_id="exec-deleted",
            cluster_id=cluster_id,
        )
    survivor = service.share_resource(
        subject_type="user",
        subject_id=grantee,
        role_id=role_id,
        resource_type="deployment_execution",
        resource_id="exec-kept",
        cluster_id=cluster_id,
    )

    assert service.revoke_resource_shares(resource_type="deployment_execution", resource_ids=["exec-deleted"]) == 2
    assert service.list_resource_shares("deployment_execution", "exec-deleted") == []
    assert [binding.id for _, binding in service.list_resource_shares("deployment_execution", "exec-kept")] == [
        survivor.id
    ]


@pytest.mark.parametrize("subject_type", ["user", "group"])
@pytest.mark.parametrize("mismatch", ["resource_id", "resource_type", "cluster_id", "global", "cluster", None])
def test_share_revocation_is_bound_to_authorized_resource(subject_type, mismatch):
    from llm_d_bench.core.exceptions import NotFoundError

    service = default_service()
    cluster_id = _cluster()
    role_id = _role({"deployment:run:read"})
    if subject_type == "user":
        subject_id = _reachable_user(role_id, cluster_id, f"revoke-{uuid4().hex[:6]}")
        add = service.add_user_binding
        dao = service.user_binding_dao
    else:
        subject_id = GroupDao().create(GroupRecord(name=f"revoke-{uuid4().hex[:6]}")).id
        add = service.add_group_binding
        dao = service.group_binding_dao

    scope = dict(scope_type="resource", scope_cluster_id=cluster_id,
                 scope_resource_type="deployment_execution", scope_resource_id="exec-owned")
    if mismatch in {"global", "cluster"}:
        scope.update(scope_type=mismatch, scope_resource_type=None, scope_resource_id=None)
        if mismatch == "global":
            scope["scope_cluster_id"] = None
    binding = add(subject_id, role_id, **scope)
    authorized = dict(resource_type="deployment_execution", resource_id="exec-owned", cluster_id=cluster_id)
    if mismatch in authorized:
        authorized[mismatch] = "other-resource"

    if mismatch is None:
        service.revoke_resource_share(binding.id, **authorized)
        assert dao.get(binding.id) is None
    else:
        with pytest.raises(NotFoundError, match="binding not found"):
            service.revoke_resource_share(binding.id, **authorized)
        assert dao.get(binding.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type", ["deployment_execution", "deployment_run"])
async def test_deployment_revoke_routes_pass_server_derived_resource_scope(monkeypatch, resource_type):
    from types import SimpleNamespace
    from llm_d_bench.deploy import router

    calls, guards = [], []
    monkeypatch.setattr(router, "default_service", lambda: SimpleNamespace(
        revoke_resource_share=lambda binding_id, **scope: calls.append((binding_id, scope))))
    monkeypatch.setattr(router, "_share_requirements_met", lambda *args, **kwargs: guards.append(kwargs))
    monkeypatch.setattr(router, "_owner_for_run", lambda run_id: ("owner", None))
    monkeypatch.setattr(router, "_require_execution", lambda execution_id: SimpleNamespace(
        run_id="run-owned", cluster_id="cluster-owned"))
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_run=lambda run_id: SimpleNamespace(
        provenance={"cluster_server_id": "cluster-owned"})))
    resource_id = "exec-owned" if resource_type == "deployment_execution" else "run-owned"
    route = router.revoke_execution_access if resource_type == "deployment_execution" else router.revoke_run_access
    response = await route(resource_id, "binding", None)
    assert response.status_code == 204
    assert guards[0]["cluster_id"] == "cluster-owned"
    assert calls == [("binding", dict(resource_type=resource_type, resource_id=resource_id, cluster_id="cluster-owned"))]
