"""Resource-scope resolution for the authorization model.

Implements design section 7.3/7.6: a subject's reachable clusters, whether a
role binding applies to a resource, and ownership coverage. All functions are
pure so they can be unit-tested and reused by both the Python and Node sides
(Node receives the resolved principal from ``/api/v1/auth/introspect``).
"""

from __future__ import annotations

from llm_d_bench.auth.contracts import Principal, ResourceRef, RoleBinding, ScopeType, action_of

#: Actions an owner *group* member must NOT inherit from ownership. The creator
#: and cluster maintainers keep them (design section 7.4).
OWNER_GROUP_EXCLUDED_ACTIONS = frozenset({"delete", "share", "grant-access"})


def reachable_clusters(principal: Principal) -> set[str] | None:
    """Cluster ids the principal can reach, or ``None`` for unrestricted.

    Only ``global`` and ``cluster`` bindings establish reachability. A
    resource-level share intentionally does **not** grant cluster access
    (design decision: shares do not cross cluster boundaries).
    """
    clusters: set[str] = set()
    for binding in principal.bindings:
        if binding.scope_type is ScopeType.GLOBAL:
            return None
        if binding.scope_type is ScopeType.CLUSTER and binding.scope_cluster_id:
            clusters.add(binding.scope_cluster_id)
    return clusters


def cluster_reachable(principal: Principal, cluster_id: str) -> bool:
    clusters = reachable_clusters(principal)
    return clusters is None or cluster_id in clusters


def binding_applies(binding: RoleBinding, resource: ResourceRef) -> bool:
    """Whether ``binding`` grants scope over ``resource``."""
    if binding.scope_type is ScopeType.GLOBAL:
        return True
    if binding.scope_type is ScopeType.CLUSTER:
        return binding.scope_cluster_id == resource.cluster_id
    if binding.scope_type is ScopeType.RESOURCE:
        return (
            binding.scope_cluster_id == resource.cluster_id
            and binding.scope_resource_type == resource.resource_type
            and binding.scope_resource_id == resource.resource_id
        )
    return False


def is_creator(principal: Principal, resource: ResourceRef) -> bool:
    return resource.owner_user_id is not None and resource.owner_user_id == principal.user_id


def is_owner_group_member(principal: Principal, resource: ResourceRef) -> bool:
    return resource.owner_group_id is not None and resource.owner_group_id in principal.group_ids


def ownership_covers(principal: Principal, resource: ResourceRef, required: str) -> bool:
    """Ownership as a scope, never as an action grant.

    The caller must first confirm the principal's role contains ``required``
    (design section 7.4); this only answers whether ownership extends the
    action's scope to the principal.
    """
    if not cluster_reachable(principal, resource.cluster_id):
        return False
    if is_creator(principal, resource):
        return True
    if is_owner_group_member(principal, resource):
        return action_of(required) not in OWNER_GROUP_EXCLUDED_ACTIONS
    return False
