"""Authorization domain contracts shared by routes, services and the policy core.

These are plain, database-agnostic value types. Persistence-row mapping happens
in the (human-approved) DAO layer; the policy engine only ever sees these.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ScopeType(StrEnum):
    """Where a role grant applies (see design section 7.3)."""

    GLOBAL = "global"
    CLUSTER = "cluster"
    RESOURCE = "resource"


def action_of(permission_code: str) -> str:
    """Return the trailing action segment of a ``domain:resource:action`` code."""
    return permission_code.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class RoleBinding:
    """A role granted to a user or group at a specific scope."""

    role: str
    scope_type: ScopeType = ScopeType.GLOBAL
    scope_cluster_id: str | None = None
    scope_resource_type: str | None = None
    scope_resource_id: str | None = None


@dataclass(frozen=True)
class ResourceRef:
    """A concrete resource instance and its ownership metadata."""

    resource_type: str
    resource_id: str
    cluster_id: str
    owner_user_id: str | None = None
    owner_group_id: str | None = None


@dataclass(frozen=True)
class Principal:
    """Authenticated subject with roles resolved to permission sets.

    ``role_permissions`` maps a role key (built-in name or custom role id) to
    its permission codes, so custom roles work without touching this module.
    ``bindings`` is the effective set of grants, already including those
    inherited from the principal's groups.
    """

    user_id: str
    username: str
    bindings: tuple[RoleBinding, ...] = ()
    role_permissions: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: role -> permissions that, for a global/cluster binding, only apply to
    #: resources the principal owns or that were explicitly shared. This is how
    #: `end-user` gets "operate your own workloads" without a cluster binding
    #: granting them on everyone else's (design section 7.4/7.8).
    role_self_scoped: Mapping[str, frozenset[str]] = field(default_factory=dict)
    group_ids: frozenset[str] = frozenset()
    authz_version: int = 1
    must_change_password: bool = False

    def is_global_admin(self) -> bool:
        """True when the principal holds the admin role at global scope."""
        return any(binding.role == "admin" and binding.scope_type is ScopeType.GLOBAL for binding in self.bindings)

    def permissions_for(self, role: str) -> frozenset[str]:
        return self.role_permissions.get(role, frozenset())

    def self_scoped_for(self, role: str) -> frozenset[str]:
        return self.role_self_scoped.get(role, frozenset())


def global_binding(role: str) -> RoleBinding:
    return RoleBinding(role=role, scope_type=ScopeType.GLOBAL)


def cluster_binding(role: str, cluster_id: str) -> RoleBinding:
    return RoleBinding(role=role, scope_type=ScopeType.CLUSTER, scope_cluster_id=cluster_id)
