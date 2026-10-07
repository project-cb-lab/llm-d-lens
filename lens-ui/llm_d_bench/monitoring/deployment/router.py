"""FastAPI routes for per-deployment observability.

A deployment is addressed only by its ``execution_id``; the run/case pair that
produced it stays inside the Deploy module. These routes report and manage the
monitoring resources attached to a deployment, not the deployment itself —
deployment facts are served by the Deploy API.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from . import service as deployment_monitoring

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/monitoring/deployments", tags=["deployment-monitoring"])

_ACTIONS = {
    "install": deployment_monitoring.install,
    "enable": deployment_monitoring.enable,
    "disable": deployment_monitoring.disable,
    "uninstall": deployment_monitoring.uninstall,
}


def _http_error(error: deployment_monitoring.DeploymentMonitoringError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get(
    "",
    summary="List deployment monitoring status for every deployment in a cluster.",
    description=(
        "List deployment monitoring status for every deployment in a cluster. Use this to discover which executions "
        "have observability installed, enabled, or unhealthy."
    ),
    operation_id="list_deployment_monitoring",
)
async def list_deployment_monitoring(cluster_id: str = Query(max_length=64)):
    """Report monitoring state for every deployment in a cluster."""
    try:
        return await deployment_monitoring.get_cluster_status(cluster_id)
    except deployment_monitoring.DeploymentMonitoringError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get(
    "/{execution_id}",
    summary="Get monitoring status for one deployment execution, optionally scoped to a cluster id.",
    description="Get monitoring status for one deployment execution, optionally scoped to a cluster id.",
    operation_id="get_deployment_monitoring",
)
async def get_deployment_monitoring(
    execution_id: str,
    cluster_id: str | None = Query(default=None, max_length=64),
):
    """Report monitoring state for one deployment execution."""
    try:
        return await deployment_monitoring.get_status(execution_id, cluster_id)
    except deployment_monitoring.DeploymentMonitoringError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/{execution_id}/{action}",
    summary="Install, enable, disable, or uninstall monitoring resources for one deployment execution.",
    description="Install, enable, disable, or uninstall monitoring resources for one deployment execution.",
    operation_id="manage_deployment_monitoring",
)
async def manage_deployment_monitoring(
    execution_id: str,
    action: str,
    cluster_id: str | None = Query(default=None, max_length=64),
):
    """Install, enable, disable, or uninstall a deployment's monitoring resources."""
    handler = _ACTIONS.get(action)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown monitoring action '{action}'")
    try:
        return await handler(execution_id, cluster_id)
    except deployment_monitoring.DeploymentMonitoringError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
