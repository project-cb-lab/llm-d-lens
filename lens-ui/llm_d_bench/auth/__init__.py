"""Authentication and authorization domain.

Persistence-backed pieces (models, DAOs, routers) are added only after the
schema changes in docs/design/auth-rbac-design.md section 18.1 are approved.
This package currently provides the schema-independent core: the permission
catalog, principal/resource contracts, scope and policy evaluation, and
credential primitives.
"""

from llm_d_bench.auth.contracts import (
    Principal,
    ResourceRef,
    RoleBinding,
    ScopeType,
    action_of,
)
from llm_d_bench.auth.permissions import (
    ALL_PERMISSIONS,
    BUILTIN_ROLE_PERMISSIONS,
    BUILTIN_ROLES,
    Permission,
    permission_granted,
    permission_matches,
    risk_of,
    role_permissions,
)
from llm_d_bench.auth.policy import Decision, accessible_cluster_ids, authorize, role_has_permission
from llm_d_bench.auth.scope import (
    binding_applies,
    cluster_reachable,
    is_creator,
    is_owner_group_member,
    ownership_covers,
    reachable_clusters,
)
from llm_d_bench.auth.security import (
    SecretCipher,
    generate_session_token,
    hash_password,
    hash_session_token,
    sign_internal,
    verify_internal,
    verify_password,
)

__all__ = [
    "ALL_PERMISSIONS",
    "BUILTIN_ROLES",
    "BUILTIN_ROLE_PERMISSIONS",
    "Decision",
    "Permission",
    "Principal",
    "ResourceRef",
    "RoleBinding",
    "ScopeType",
    "SecretCipher",
    "accessible_cluster_ids",
    "action_of",
    "authorize",
    "binding_applies",
    "cluster_reachable",
    "generate_session_token",
    "hash_password",
    "hash_session_token",
    "is_creator",
    "is_owner_group_member",
    "ownership_covers",
    "permission_granted",
    "permission_matches",
    "reachable_clusters",
    "risk_of",
    "role_has_permission",
    "role_permissions",
    "sign_internal",
    "verify_internal",
    "verify_password",
]
