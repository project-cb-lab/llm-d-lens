"""Request-scope helpers for domain routers (design section 7.6).

Routers use these to filter list responses to the caller's reachable clusters
and to reject access to a specific cluster. When authentication is disabled
the middleware injects a global admin, so ``accessible_cluster_ids`` returns
``None`` and filtering is a no-op (existing behaviour is preserved).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from fastapi import Request

from llm_d_bench.auth.contracts import Principal, ResourceRef
from llm_d_bench.auth.policy import accessible_cluster_ids, authorize
from llm_d_bench.core.exceptions import ForbiddenError

T = TypeVar("T")


def owner_for_create(principal: Principal | None) -> str | None:
    """Return the creating user's id, or ``None`` for the disabled-auth sentinel."""
    if principal is None or principal.user_id == "system":
        return None
    return principal.user_id


def resource_readable(
    principal: Principal | None,
    *,
    permission: str,
    resource_type: str,
    resource_id: str,
    cluster_id: str | None,
    owner_user_id: str | None = None,
    owner_group_id: str | None = None,
) -> bool:
    """Whether the principal may read a concrete resource instance.

    ``principal is None`` (direct handler calls / auth disabled) is unrestricted.
    """
    if principal is None:
        return True
    resource = ResourceRef(
        resource_type=resource_type,
        resource_id=resource_id,
        cluster_id=cluster_id or "",
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
    )
    return authorize(principal, permission, resource=resource).allowed


def current_principal(request: Request | None) -> Principal | None:
    """Principal for a request, or ``None`` (direct handler calls/tests)."""
    if request is None:
        return None
    return getattr(request.state, "principal", None)


def visible_cluster_ids(principal: Principal | None) -> set[str] | None:
    if principal is None:
        return None
    return accessible_cluster_ids(principal)


def cluster_allowed(principal: Principal | None, cluster_id: str | None) -> bool:
    allowed = visible_cluster_ids(principal)
    if allowed is None or cluster_id is None:
        return True
    return cluster_id in allowed


def require_cluster_access(principal: Principal | None, cluster_id: str | None) -> None:
    if not cluster_allowed(principal, cluster_id):
        raise ForbiddenError("cluster is not accessible")


def filter_by_cluster(
    items: Iterable[T], get_cluster_id: Callable[[T], str | None], principal: Principal | None
) -> list[T]:
    allowed = visible_cluster_ids(principal)
    materialized = list(items)
    if allowed is None:
        return materialized
    return [item for item in materialized if get_cluster_id(item) in allowed]


def effective_cluster_filter(principal: Principal | None, requested: Iterable[str] | None) -> list[str] | None:
    """Combine a caller-requested cluster-id filter with what they can reach.

    ``None`` means "no cluster filter" (unrestricted principal, no request).
    Otherwise returns the intersection -- a maintainer requesting a cluster they
    don't reach gets an empty result rather than someone else's data, and a
    maintainer requesting nothing is scoped to every cluster they can reach.
    """
    allowed = visible_cluster_ids(principal)
    requested_ids = list(requested) if requested else None
    if allowed is None:
        return requested_ids
    if requested_ids:
        return sorted(set(requested_ids) & allowed)
    return sorted(allowed)
