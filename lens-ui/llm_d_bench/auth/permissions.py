"""Authorization permission catalog and built-in role definitions.

This module is the single source of truth for permission codes
(``<domain>:<resource>:<action>``) and for the three built-in roles. The
catalog lives in code, not in a table; the ``role_permissions`` table only
stores the binding of a role to a subset of these codes.

Design reference: docs/design/auth-rbac-design.md sections 6 and 7.
"""

from __future__ import annotations

from dataclasses import dataclass

READ = "read"
WRITE = "write"
APPROVE = "approve"

BUILTIN_ROLE_ADMIN = "admin"
BUILTIN_ROLE_MAINTAINER = "maintainer"
BUILTIN_ROLE_END_USER = "end-user"

BUILTIN_ROLES = (BUILTIN_ROLE_ADMIN, BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_END_USER)


@dataclass(frozen=True)
class Permission:
    """A single action permission.

    ``risk`` mirrors the MCP tool risk vocabulary (``read``/``write``/
    ``approve``) so UI gates and assistant tool tiers can share one language.
    """

    code: str
    domain: str
    description: str
    risk: str


def _permission(code: str, description: str, risk: str = WRITE) -> Permission:
    return Permission(code=code, domain=code.split(":", 1)[0], description=description, risk=risk)


# --- catalog -----------------------------------------------------------------
# Keep grouped by domain; the generated code is validated by test_permissions.
_PERMISSIONS: tuple[Permission, ...] = (
    # identity & administration
    _permission("user:user:read", "View users", READ),
    _permission("user:user:create", "Create users"),
    _permission("user:user:update", "Edit users"),
    _permission("user:user:delete", "Delete users"),
    _permission("user:user:reset-password", "Reset a user's password"),
    _permission("group:group:read", "View groups", READ),
    _permission("group:group:create", "Create groups"),
    _permission("group:group:update", "Edit groups"),
    _permission("group:group:delete", "Delete groups"),
    _permission("group:group:manage-members", "Manage group members"),
    _permission("role:role:read", "View roles", READ),
    _permission("role:role:create", "Create roles"),
    _permission("role:role:update", "Edit role permissions"),
    _permission("role:role:delete", "Delete roles"),
    _permission("role:binding:manage", "Manage user/group role bindings"),
    _permission("idp:provider:read", "View identity providers", READ),
    _permission("idp:provider:configure", "Configure identity providers and test connections"),
    _permission("idp:mapping:manage", "Manage directory group mappings"),
    _permission("idp:sync:execute", "Run an external directory sync"),
    _permission("session:session:read", "View sessions", READ),
    _permission("session:session:revoke", "Revoke sessions"),
    _permission("audit:log:read", "View the audit log", READ),
    _permission("system:database:read", "View database configuration", READ),
    _permission("system:database:configure", "Configure the database"),
    _permission("system:secret:read", "View the stored-secret master key status", READ),
    _permission("system:secret:manage", "Rotate or clear the stored-secret master key"),
    # cluster
    _permission("cluster:cluster:read", "View clusters", READ),
    _permission("cluster:cluster:create", "Register clusters"),
    _permission("cluster:cluster:update", "Edit clusters"),
    _permission("cluster:cluster:delete", "Delete clusters"),
    _permission("cluster:cluster:grant-access", "Grant cluster access"),
    _permission("cluster:hf-token:create", "Create the HF_TOKEN Secret"),
    _permission("cluster:node:operate", "Cordon/uncordon nodes"),
    _permission("cluster:session:connect", "Open a cluster session"),
    _permission("cluster:port-forward:connect", "Open cluster port-forwards"),
    _permission("cluster:planning:read", "Read cluster planning discovery", READ),
    _permission("cluster:bootstrap:read", "View cluster bootstrap status", READ),
    _permission("cluster:bootstrap:execute", "Run cluster bootstrap"),
    _permission("cluster:endpoint:read", "Discover cluster endpoints", READ),
    # storage
    _permission("storage:volume:read", "View storage volumes", READ),
    _permission("storage:volume:create", "Create storage volumes"),
    _permission("storage:volume:delete", "Delete storage volumes"),
    _permission("storage:volume:acknowledge", "Acknowledge storage node changes"),
    _permission("storage:volume:scan", "Rescan storage volumes for models"),
    # model cache
    _permission("model-cache:entry:read", "View model cache entries", READ),
    _permission("model-cache:entry:create", "Download models"),
    _permission("model-cache:entry:retry", "Retry model downloads"),
    _permission("model-cache:entry:sync", "Sync models to nodes"),
    _permission("model-cache:entry:delete", "Delete model cache entries"),
    # deployment
    _permission("deployment:run:read", "View deployments", READ),
    _permission("deployment:run:create", "Create deployments"),
    _permission("deployment:run:cancel", "Cancel deployments"),
    _permission("deployment:run:share", "Share deployments"),
    _permission("deployment:execution:update", "Edit deployment metadata"),
    _permission("deployment:execution:delete", "Delete deployments"),
    _permission("deployment:execution:connect", "Connect to deployment endpoints"),
    _permission("deployment:case:execute", "Refresh/stop/restart deployment instances"),
    _permission("deployment:session:rebind", "Rebind a deployment cluster session"),
    _permission("deployment:agentic:read", "View agentic deployments", READ),
    _permission("deployment:agentic:create", "Create/refine/select agentic deployments"),
    _permission("deployment:agentic:approve", "Approve agentic deployments", APPROVE),
    # evaluate
    _permission("evaluate:run:read", "View evaluations", READ),
    _permission("evaluate:run:create", "Benchmark an existing deployment"),
    _permission("evaluate:workflow:create", "Start evaluation workflows that deploy a new deployment"),
    _permission("evaluate:run:cancel", "Cancel evaluations"),
    _permission("evaluate:run:retry", "Retry evaluations"),
    _permission("evaluate:run:delete", "Delete evaluations"),
    # configuration
    _permission("configuration:artifact:read", "View configuration artifacts", READ),
    _permission("configuration:artifact:save", "Save configuration artifacts"),
    _permission("configuration:artifact:delete", "Delete configuration artifacts"),
    _permission("configuration:artifact:render", "Resolve/render configurations"),
    _permission("candidate:candidate:search", "Search candidates"),
    _permission("guide:plan:execute", "Run guide planning"),
    # simulation
    _permission("simulation:task:read", "View simulations", READ),
    _permission("simulation:task:create", "Create simulations"),
    _permission("simulation:task:stop", "Stop simulations"),
    _permission("simulation:task:rerun", "Rerun simulations"),
    _permission("simulation:task:delete", "Delete simulations"),
    _permission("simulation:dataset:download", "Download trace datasets", READ),
    _permission("simulation:artifact:download", "Download simulation artifacts", READ),
    # AI providers
    _permission("ai-provider:provider:read", "View external providers", READ),
    _permission("ai-provider:provider:create", "Create external providers"),
    _permission("ai-provider:provider:update", "Edit external providers"),
    _permission("ai-provider:provider:delete", "Delete external providers"),
    _permission("ai-provider:provider:test", "Test external provider connections"),
    _permission("ai-provider:provider:chat", "Use external providers for chat"),
    # model service
    _permission("model-service:token:manage", "Manage one's own model access tokens", READ),
    _permission("model-service:inference:use", "Call published model services", READ),
    _permission("model-service:usage:read", "View one's own model usage", READ),
    _permission("model-service:group:read", "View model routing groups and members", READ),
    _permission("model-service:group:manage", "Create/edit/delete/enable-disable model services"),
    _permission("model-service:member:manage", "Publish/edit/remove a cluster deployment as a service provider"),
    _permission("model-service:gateway:read", "View model gateway data-plane status", READ),
    _permission("model-service:gateway:manage", "Install/uninstall/reload the shared Edge Envoy data plane"),
    _permission("model-service:router:manage", "Install/uninstall/start/stop a cluster's Agent Router"),
    _permission("usage:report:read", "View model usage across users/groups", READ),
    # hardware
    _permission("hardware:profile:read", "View hardware profiles", READ),
    # monitoring
    _permission("monitoring:cluster-stack:read", "View the cluster monitoring stack", READ),
    _permission("monitoring:cluster-stack:install", "Install the cluster monitoring stack"),
    _permission("monitoring:operation:read", "View monitoring operations", READ),
    _permission("monitoring:accelerator:read", "View accelerator observability", READ),
    _permission("monitoring:accelerator:install", "Install accelerator observability"),
    _permission("monitoring:gpu-driver:read", "View GPU driver status", READ),
    _permission("monitoring:gpu-driver:install", "Install the GPU driver"),
    _permission("monitoring:deployment:read", "View deployment monitoring", READ),
    _permission("monitoring:deployment:action", "Install/toggle deployment monitoring"),
    _permission("monitoring:profiling:read", "View profiling", READ),
    # playground & MCP
    _permission("playground:chat:use", "Use the Lens Assistant"),
    _permission("playground:tool:write", "Use Assistant write tools"),
    _permission("playground:tool:approve", "Use Assistant approve tools", APPROVE),
    # node-native capabilities
    _permission("remote-deploy:target:read", "View remote deploy targets", READ),
    _permission("remote-deploy:target:connect", "Probe remote deploy targets"),
    _permission("remote-deploy:deploy:execute", "Run remote deployments"),
    _permission("remote-deploy:deploy:teardown", "Tear down remote deployments"),
    _permission("deploy-poc:job:read", "View deploy PoC jobs", READ),
    _permission("deploy-poc:job:execute", "Run deploy PoC jobs"),
    _permission("deploy-poc:job:teardown", "Tear down deploy PoC jobs"),
    _permission("config:runtime:read", "Read runtime config", READ),
    _permission("mcp:tool:invoke", "Invoke MCP tools"),
)

ALL_PERMISSIONS: dict[str, Permission] = {permission.code: permission for permission in _PERMISSIONS}

assert len(ALL_PERMISSIONS) == len(_PERMISSIONS), "duplicate permission code in catalog"

_READ_SUFFIXES = (":read", ":download")
_APPROVE_SUFFIXES = (":approve",)


def risk_of(code: str) -> str:
    """Return the risk tier for a code, defaulting by action when unknown."""
    known = ALL_PERMISSIONS.get(code)
    if known is not None:
        return known.risk
    if code.endswith(_APPROVE_SUFFIXES):
        return APPROVE
    return READ if code.endswith(_READ_SUFFIXES) else WRITE


# --- built-in roles ----------------------------------------------------------
_ADMIN_DENIED: frozenset[str] = frozenset()


def _permissions_named(*codes: str) -> frozenset[str]:
    missing = [code for code in codes if code not in ALL_PERMISSIONS]
    if missing:
        raise ValueError(f"unknown permission codes: {missing}")
    return frozenset(codes)


#: maintainer may operate infrastructure and workloads but cannot manage
#: identities, roles, identity providers, audit or the database configuration.
#: Model Service write access is split: creating/editing/deleting/enabling-
#: disabling the model service itself (`group:manage`) and the shared,
#: cluster-independent Edge Envoy data plane (`gateway:manage`, e.g. reload,
#: edge start/stop/restart/port, global reconcile/probe) are admin-only.
#: Maintainers keep read access plus the ability to publish/edit/remove a
#: deployment as a service provider (`member:manage`) and install/uninstall/
#: start/stop a cluster's own Agent Router (`router:manage`), both of which
#: are cluster-scoped to clusters they reach (explicit product decision,
#: superseding the earlier "maintainer manages workloads" default only for
#: the group/gateway slice of this domain).
_MAINTAINER_DENIED = _permissions_named(
    "user:user:read",
    "user:user:create",
    "user:user:update",
    "user:user:delete",
    "user:user:reset-password",
    "group:group:read",
    "group:group:create",
    "group:group:update",
    "group:group:delete",
    "group:group:manage-members",
    "role:role:read",
    "role:role:create",
    "role:role:update",
    "role:role:delete",
    "role:binding:manage",
    "idp:provider:read",
    "idp:provider:configure",
    "idp:mapping:manage",
    "idp:sync:execute",
    "session:session:revoke",
    "audit:log:read",
    "system:database:read",
    "system:database:configure",
    "system:secret:read",
    "system:secret:manage",
    "model-service:group:manage",
    "model-service:gateway:manage",
)

#: end-user is a model-service *consumer* only (product decision D15 in
#: docs/design/model-service-v2-design.md): manage its own access tokens,
#: call models it is authorized for, and read its own usage. Workloads,
#: evaluations, simulations, clusters, storage, model cache, external
#: providers, observability and administration belong to maintainer/admin.
_END_USER_ALLOWED = _permissions_named(
    "model-service:token:manage",
    "model-service:inference:use",
    "model-service:usage:read",
)

BUILTIN_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    BUILTIN_ROLE_ADMIN: frozenset(ALL_PERMISSIONS) - _ADMIN_DENIED,
    BUILTIN_ROLE_MAINTAINER: frozenset(ALL_PERMISSIONS) - _MAINTAINER_DENIED,
    BUILTIN_ROLE_END_USER: _END_USER_ALLOWED,
}

#: end-user's model-service actions are self-scoped: a cluster binding does not
#: grant them on models the user is not authorized for; it needs ownership or an
#: explicit resource share on the target execution.
_END_USER_SELF_SCOPED = _permissions_named(
    "model-service:token:manage",
    "model-service:inference:use",
    "model-service:usage:read",
)

BUILTIN_ROLE_SELF_SCOPED: dict[str, frozenset[str]] = {
    BUILTIN_ROLE_ADMIN: frozenset(),
    BUILTIN_ROLE_MAINTAINER: frozenset(),
    BUILTIN_ROLE_END_USER: _END_USER_SELF_SCOPED,
}


def role_permissions(role: str) -> frozenset[str]:
    """Return the permission codes for a built-in role (empty for custom roles)."""
    return BUILTIN_ROLE_PERMISSIONS.get(role, frozenset())


def role_self_scoped(role: str) -> frozenset[str]:
    """Permissions that require ownership/resource-share even under a cluster binding."""
    return BUILTIN_ROLE_SELF_SCOPED.get(role, frozenset())


def permission_matches(bound: str, required: str) -> bool:
    """Segment-wildcard permission matching.

    ``bound`` may use ``*`` per segment, e.g. ``deployment:*:*`` matches
    ``deployment:run:create`` and ``*:*:read`` matches every read action.
    """
    if bound == required:
        return True
    bound_parts = bound.split(":")
    required_parts = required.split(":")
    if len(bound_parts) != len(required_parts):
        return False
    return all(pattern == "*" or pattern == value for pattern, value in zip(bound_parts, required_parts, strict=True))


def permission_granted(granted: frozenset[str] | set[str], required: str) -> bool:
    """True when any granted pattern covers ``required``."""
    return any(permission_matches(bound, required) for bound in granted)
