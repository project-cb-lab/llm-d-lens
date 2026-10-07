"""Narrow planning reads over the existing private cluster-session boundary."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException

from llm_d_bench.cluster import sessions
from llm_d_bench.utils.kubernetes import run_kubectl

router = APIRouter()
_READS = {
    "version": ["version", "-o", "json"],
    "nodes": ["get", "nodes", "-o", "json"],
    "deviceclasses": ["get", "deviceclasses.resource.k8s.io", "-o", "json"],
    "resourceslices": ["get", "resourceslices.resource.k8s.io", "-o", "json"],
    "runtimeclasses": ["get", "runtimeclasses.node.k8s.io", "-o", "json"],
}


@router.get(
    "/sessions/{session_id}/planning-discovery/{resource}",
    summary="Read one planning discovery resource for an active cluster session.",
    operation_id="read_cluster_planning_discovery",
)
async def planning_discovery(
    session_id: str,
    resource: Literal["version", "nodes", "deviceclasses", "resourceslices", "runtimeclasses"],
) -> dict[str, object]:
    try:
        session = sessions.require_active_session(session_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Cluster session is no longer active") from error
    try:
        # The shared read facade owns SDK/CLI selection and compatibility. Never
        # accept kubeconfig paths, API paths, or command arguments from callers.
        result = await run_kubectl(_READS[resource], timeout=10, cluster_id=session.id)
        payload = json.loads(result.stdout) if result.ok else None
        return {"result": payload if isinstance(payload, dict) else None}
    except Exception:
        # Planning treats unavailable optional APIs and failed required reads as
        # null; the Node caller decides whether preflight can succeed.
        return {"result": None}
