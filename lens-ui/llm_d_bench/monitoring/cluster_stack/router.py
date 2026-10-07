"""FastAPI routes for cluster monitoring stack management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Query

from .errors import ClusterStackError
from .models import (
    ClusterStackInstallRequest,
    ClusterStackLinksResponse,
    ClusterStackOperationResponse,
    ClusterStackPreflightResponse,
    ClusterStackStatusResponse,
)
from .service import get_links, get_operation, get_status, install, preflight, validate_namespace

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/monitoring/cluster-stack", tags=["cluster-monitoring-stack"])


def _http_error(error: ClusterStackError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail())


@router.get(
    "/status",
    response_model=ClusterStackStatusResponse,
    summary=(
        "Check whether the cluster-level Prometheus/Grafana monitoring stack (kube-prometheus-stack) is installed "
        "and ready."
    ),
    description=(
        "Check whether the cluster-level Prometheus/Grafana monitoring stack (kube-prometheus-stack) is installed "
        "and ready."
    ),
    operation_id="get_cluster_stack_status",
)
async def status(
    namespace: str = Query(default="llm-d-monitoring", max_length=63),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> ClusterStackStatusResponse:
    try:
        return await get_status(validate_namespace(namespace), cluster_id=cluster_id)
    except ClusterStackError as error:
        raise _http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error
    except Exception as error:
        logger.exception("Unable to query cluster monitoring stack")
        raise HTTPException(
            status_code=502,
            detail={"code": "CLUSTER_QUERY_FAILED", "message": str(error), "retryable": True},
        ) from error


@router.post(
    "/installations/preflight",
    response_model=ClusterStackPreflightResponse,
    summary="Run cluster monitoring stack preflight checks without installing anything.",
    description=(
        "Run cluster monitoring stack preflight checks without installing anything. "
        "Use this before starting a Prometheus/Grafana stack installation."
    ),
    operation_id="preflight_cluster_stack_install",
)
async def installation_preflight(
    request: ClusterStackInstallRequest,
    cluster_id: str | None = Query(default=None, max_length=64),
) -> ClusterStackPreflightResponse:
    try:
        return await preflight(request, cluster_id=cluster_id)
    except ClusterStackError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error


@router.post(
    "/installations",
    response_model=ClusterStackOperationResponse,
    status_code=202,
    summary="Install or reinstall the cluster-level monitoring stack.",
    description=(
        "Install or reinstall the cluster-level monitoring stack. "
        "Use this when Prometheus/Grafana cluster monitoring should be provisioned or repaired."
    ),
    operation_id="install_cluster_stack",
)
async def create_installation(
    request: ClusterStackInstallRequest,
    idempotency_key: str | None = Header(default=None, max_length=128),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> ClusterStackOperationResponse:
    try:
        return await install(request, idempotency_key=idempotency_key, cluster_id=cluster_id)
    except ClusterStackError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error


@router.get(
    "/operations/{operation_id}",
    response_model=ClusterStackOperationResponse,
    summary="Get the status and logs of one cluster monitoring stack installation operation.",
    description="Get the status and logs of one cluster monitoring stack installation operation.",
    operation_id="get_cluster_stack_operation",
)
async def operation(operation_id: str) -> ClusterStackOperationResponse:
    try:
        return get_operation(operation_id)
    except ClusterStackError as error:
        raise _http_error(error) from error


@router.get(
    "/links",
    response_model=ClusterStackLinksResponse,
    summary="Get resolved Grafana and Prometheus links for the cluster monitoring stack.",
    description="Get resolved Grafana and Prometheus links for the cluster monitoring stack.",
    operation_id="get_cluster_stack_links",
)
async def links(
    namespace: str = Query(default="llm-d-monitoring", max_length=63),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> ClusterStackLinksResponse:
    try:
        return await get_links(validate_namespace(namespace), cluster_id=cluster_id)
    except ClusterStackError as error:
        raise _http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error
    except Exception as error:
        logger.exception("Unable to resolve cluster monitoring stack links")
        raise HTTPException(
            status_code=502,
            detail={"code": "CLUSTER_QUERY_FAILED", "message": str(error), "retryable": True},
        ) from error
