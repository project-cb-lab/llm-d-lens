"""FastAPI routes for deployment profiling."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from llm_d_bench.monitoring.deployment.service import DeploymentMonitoringError

from .models import FlowMapResponse
from .service import build_flow_map

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/monitoring/profiling", tags=["deployment-profiling"])


@router.get(
    "/flow-map",
    response_model=FlowMapResponse,
    summary="Query the Prometheus-backed GPU idle/utilization flow-map for a specific deployment execution.",
    description="Query the Prometheus-backed GPU idle/utilization flow-map for a specific deployment execution ",
    operation_id="get_flow_map_metrics",
)
async def flow_map(
    execution_id: str = Query(max_length=128),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> FlowMapResponse:
    try:
        return await build_flow_map(execution_id, cluster_id)
    except DeploymentMonitoringError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except Exception as error:
        logger.exception("Unable to build deployment flow map")
        raise HTTPException(
            status_code=502,
            detail={"code": "FLOW_MAP_FAILED", "message": str(error), "retryable": True},
        ) from error
