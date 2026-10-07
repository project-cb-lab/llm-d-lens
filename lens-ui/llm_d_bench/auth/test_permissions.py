import pytest

from llm_d_bench.auth.permissions import (
    ALL_PERMISSIONS,
    BUILTIN_ROLE_ADMIN,
    BUILTIN_ROLE_END_USER,
    BUILTIN_ROLE_MAINTAINER,
    BUILTIN_ROLE_PERMISSIONS,
    READ,
    WRITE,
    permission_granted,
    permission_matches,
    risk_of,
)


def test_catalog_codes_are_well_formed_and_unique():
    assert len(ALL_PERMISSIONS) == len(set(ALL_PERMISSIONS))
    for code, permission in ALL_PERMISSIONS.items():
        parts = code.split(":")
        assert len(parts) == 3, f"{code} must have three segments"
        assert permission.domain == parts[0]
        assert permission.description
        assert permission.risk in {READ, WRITE, "approve"}


def test_admin_has_every_permission():
    assert BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_ADMIN] == frozenset(ALL_PERMISSIONS)


@pytest.mark.parametrize(
    "code",
    [
        "user:user:create",
        "role:binding:manage",
        "idp:provider:configure",
        "system:database:configure",
        "session:session:revoke",
        "audit:log:read",
        "model-service:group:manage",
        "model-service:gateway:manage",
    ],
)
def test_maintainer_cannot_administer(code):
    assert code not in BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER]


@pytest.mark.parametrize(
    "code",
    [
        "cluster:cluster:create",
        "cluster:cluster:delete",
        "storage:volume:create",
        "storage:volume:delete",
        "monitoring:cluster-stack:install",
        "monitoring:accelerator:install",
        "monitoring:gpu-driver:install",
        "ai-provider:provider:create",
        "remote-deploy:deploy:execute",
        "model-service:gateway:read",
        "model-service:group:read",
        "model-service:member:manage",
        "model-service:router:manage",
    ],
)
def test_maintainer_can_operate_infrastructure(code):
    assert code in BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER]


def test_group_update_route_requires_group_manage_not_member_manage():
    """A group's selection policy is a group-identity field: a maintainer may
    retune a provider's own policy *parameter* (e.g. weight, via /admin/members,
    `model-service:member:manage`) but must not be able to change the group's
    selection policy itself, so PATCH /admin/groups/{id} stays group:manage-only."""
    from llm_d_bench.auth.routes import resolve_route_permission

    found, permission = resolve_route_permission("PATCH", "/api/v1/model-service/admin/groups/g1")
    assert found
    assert permission == "model-service:group:manage"


@pytest.mark.parametrize(
    "code",
    [
        "cluster:cluster:create",
        "cluster:cluster:delete",
        "cluster:cluster:grant-access",
        "cluster:cluster:read",
        "cluster:session:connect",
        "deployment:run:create",
        "deployment:run:read",
        "deployment:run:cancel",
        "deployment:execution:delete",
        "deployment:agentic:create",
        "deployment:agentic:approve",
        "deployment:agentic:read",
        "evaluate:workflow:create",
        "evaluate:run:read",
        "evaluate:run:create",
        "simulation:task:read",
        "simulation:task:create",
        "configuration:artifact:read",
        "candidate:candidate:search",
        "guide:plan:execute",
        "storage:volume:read",
        "storage:volume:delete",
        "storage:volume:create",
        "model-cache:entry:read",
        "model-cache:entry:create",
        "monitoring:cluster-stack:read",
        "monitoring:cluster-stack:install",
        "monitoring:deployment:read",
        "monitoring:deployment:action",
        "monitoring:profiling:read",
        "ai-provider:provider:read",
        "ai-provider:provider:chat",
        "ai-provider:provider:create",
        "playground:chat:use",
        "session:session:read",
        "session:session:revoke",
        "user:user:read",
        "role:binding:manage",
    ],
)
def test_end_user_is_restricted(code):
    assert code not in BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_END_USER]


@pytest.mark.parametrize(
    "code",
    [
        "model-service:token:manage",
        "model-service:inference:use",
        "model-service:usage:read",
    ],
)
def test_end_user_can_consume_model_services(code):
    assert code in BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_END_USER]


def test_end_user_has_no_monitoring_or_workload_scope():
    granted = BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_END_USER]
    assert "monitoring:deployment:action" not in granted
    assert "monitoring:deployment:read" not in granted
    assert "monitoring:cluster-stack:install" not in granted
    assert granted == frozenset(
        {"model-service:token:manage", "model-service:inference:use", "model-service:usage:read"}
    )


@pytest.mark.parametrize(
    ("bound", "required", "expected"),
    [
        ("deployment:run:create", "deployment:run:create", True),
        ("deployment:*:*", "deployment:run:create", True),
        ("*:*:read", "deployment:run:read", True),
        ("*:*:*", "anything:here:now", True),
        ("deployment:run:read", "deployment:run:create", False),
        ("deployment:*", "deployment:run:create", False),
        ("deployment:run:*", "deployment:execution:create", False),
    ],
)
def test_permission_matches(bound, required, expected):
    assert permission_matches(bound, required) is expected


def test_permission_granted_uses_wildcards():
    granted = frozenset({"deployment:run:read", "cluster:*:*"})
    assert permission_granted(granted, "cluster:cluster:delete")
    assert not permission_granted(granted, "storage:volume:delete")


def test_risk_of_defaults_for_unknown_codes():
    assert risk_of("monitoring:cluster-stack:read") == READ
    assert risk_of("evaluate:run:create") == WRITE
    assert risk_of("deployment:agentic:approve") == "approve"
