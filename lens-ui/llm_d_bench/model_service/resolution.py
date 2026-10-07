"""Resolve a published Model Service group to a concrete, healthy backend.

Evaluation ("use existing endpoint") and Simulation ("Cluster Deployment") both
let a caller target an already-published Model Service instead of a raw
deployment execution, so they only ever reach a backend that is continuously
health-probed (``ModelService.probe_members``) and currently authorized for
the caller -- a raw deployment execution's own ready-ness has no such live
signal. This module centralizes that resolution so both callers share one
implementation (see docs/design/model-service-gateway-deployment.md).
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from llm_d_bench.auth.access import current_principal


def resolve_model_service_target(group_id: str, http_request: Request | None) -> tuple[str, str]:
    """Pick a currently healthy, authorized member of a Model Service group.

    Returns ``(execution_id, published_name)``. Raises HTTPException(404) when the
    group is missing/inactive and HTTPException(409) when it has no member the
    caller may call right now (for example every member is unhealthy).
    """
    from llm_d_bench.model_service.selection import select_member  # noqa: PLC0415
    from llm_d_bench.model_service.service import default_service as default_model_service  # noqa: PLC0415

    service = default_model_service()
    group = service.groups.get(group_id)
    if group is None or group.status != "active":
        raise HTTPException(status_code=404, detail="model service not found")
    principal = current_principal(http_request)
    members = service.authorized_members(group, principal)
    if not members:
        raise HTTPException(status_code=409, detail="model service has no healthy, authorized member right now")
    user_id = principal.user_id if principal is not None else "system"
    member = select_member(members, group.selection_policy, user_id=user_id, group_id=group.id)
    return member.execution_id, group.name
