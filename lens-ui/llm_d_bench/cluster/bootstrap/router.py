"""HTTP routes for the Kubespray bootstrap sub-wizard (design §5)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from llm_d_bench.cluster.bootstrap import service
from llm_d_bench.cluster.bootstrap.dto import (
    BootstrapCheckResult,
    BootstrapCreateRequest,
    BootstrapCreateResponse,
    BootstrapHostKeyRequest,
    BootstrapHostKeyResponse,
    BootstrapNodeResult,
    BootstrapPreflightRequest,
    BootstrapPreflightResponse,
    BootstrapStatusResponse,
)
from llm_d_bench.cluster.bootstrap.models import BootstrapNode

router = APIRouter(prefix="/api/cluster/bootstrap", tags=["cluster-bootstrap"])


def _node_result(node: BootstrapNode) -> BootstrapNodeResult:
    return BootstrapNodeResult(
        host=node.host,
        roles=node.roles,
        state=node.preflight_state,
        error=node.preflight_error,
        checks=[
            BootstrapCheckResult(id=item.id, label=item.label, status=item.status, detail=item.detail)
            for item in node.preflight_checks
        ],
    )


@router.post(
    "/host-keys",
    response_model=BootstrapHostKeyResponse,
    summary="Discover SSH host-key fingerprints for bootstrap targets.",
    description="Read each target's SSH host key without authenticating, for explicit user verification.",
    operation_id="discover_cluster_bootstrap_host_keys",
)
async def discover_host_keys(payload: BootstrapHostKeyRequest) -> BootstrapHostKeyResponse:
    try:
        items = await service.discover_host_keys(payload.nodes)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return BootstrapHostKeyResponse(items=items)


@router.post(
    "/preflight",
    response_model=BootstrapPreflightResponse,
    summary="Run Kubespray bootstrap preflight checks against one or more target machines.",
    description=(
        "Run Kubespray bootstrap preflight checks against one or more target machines. "
        "Use this before starting a real cluster bootstrap job."
    ),
    operation_id="preflight_cluster_bootstrap",
)
async def preflight(payload: BootstrapPreflightRequest) -> BootstrapPreflightResponse:
    try:
        nodes = await service.run_preflight_check(payload.nodes, payload.host_keys)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    items = [_node_result(node) for node in nodes]
    return BootstrapPreflightResponse(items=items, all_passed=all(item.state == "passed" for item in items))


@router.post(
    "",
    response_model=BootstrapCreateResponse,
    status_code=202,
    summary="Start a Kubespray-based cluster bootstrap job.",
    description=(
        "Start a Kubespray-based cluster bootstrap job. Use this after preflight succeeds and you are ready to "
        "provision the cluster."
    ),
    operation_id="start_cluster_bootstrap",
)
async def start_bootstrap(payload: BootstrapCreateRequest) -> BootstrapCreateResponse:
    try:
        job = service.create_job(payload.nodes, payload.host_keys, proxy=payload.proxy)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    asyncio.create_task(service.run_bootstrap_job(job.id))
    return BootstrapCreateResponse(bootstrap_id=job.id)


@router.get(
    "/{bootstrap_id}",
    response_model=BootstrapStatusResponse,
    summary=(
        "Get the current status of a cluster bootstrap job, including phase, log tail, and one-time kubeconfig "
        "handoff when the job succeeds."
    ),
    description=(
        "Get the current status of a cluster bootstrap job, including phase, log tail, and one-time kubeconfig "
        "handoff when the job succeeds."
    ),
    operation_id="get_cluster_bootstrap_status",
)
async def bootstrap_status(bootstrap_id: str) -> BootstrapStatusResponse:
    try:
        job = service.get_job(bootstrap_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="bootstrap job not found") from error
    kubeconfig = None
    if job.phase == "succeeded":
        # One-shot consumption (design §6): the caller that first observes
        # "succeeded" gets the kubeconfig; the job is then torn down.
        kubeconfig = service.consume_kubeconfig(bootstrap_id)
    return BootstrapStatusResponse(
        id=job.id, phase=job.phase, log_tail=job.log_tail, error=job.error, kubeconfig=kubeconfig
    )


@router.post(
    "/{bootstrap_id}/cancel",
    status_code=204,
    summary="Cancel a running cluster bootstrap job.",
    description=(
        "Cancel a running cluster bootstrap job. Use this when the current bootstrap should stop and not continue "
        "provisioning."
    ),
    operation_id="cancel_cluster_bootstrap",
)
async def cancel_bootstrap(bootstrap_id: str) -> None:
    try:
        service.cancel_job(bootstrap_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="bootstrap job not found") from error
