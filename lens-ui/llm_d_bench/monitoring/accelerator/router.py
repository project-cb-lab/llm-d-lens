"""FastAPI routes for accelerator observability management."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query

from .errors import AcceleratorError
from .models import (
    AcceleratorCapabilitiesResponse,
    AcceleratorInstallRequest,
    AcceleratorLinksResponse,
    AcceleratorOperationResponse,
    AcceleratorPreflightResponse,
    AcceleratorStatusResponse,
    GpuAccess,
)
from .service import (
    capabilities,
    get_links,
    get_operation,
    get_status,
    install,
    preflight,
    validate_namespace,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/monitoring/accelerators", tags=["accelerator-observability"])


def _http_error(error: AcceleratorError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail())


@router.get(
    "",
    response_model=AcceleratorCapabilitiesResponse,
    summary=(
        "List accelerator observability capabilities Lens supports, including access modes and default namespaces."
    ),
    description=(
        "List accelerator observability capabilities Lens supports, including access modes and default namespaces."
    ),
    operation_id="list_monitoring_accelerators",
)
async def list_accelerators() -> AcceleratorCapabilitiesResponse:
    return capabilities()


@router.get(
    "/{accelerator}/status",
    response_model=AcceleratorStatusResponse,
    summary=(
        "Get observability status for one accelerator family, including release, component, and access-mode health."
    ),
    description=(
        "Get observability status for one accelerator family, including release, component, and access-mode health."
    ),
    operation_id="get_accelerator_monitoring_status",
)
async def status(
    accelerator: str,
    namespace: Annotated[str, Query(max_length=63)] = "intel-xpumd",
    access_mode: Annotated[GpuAccess | None, Query()] = None,
    cluster_id: str | None = Query(default=None, max_length=64),
) -> AcceleratorStatusResponse:
    try:
        return await get_status(accelerator, validate_namespace(namespace), access_mode, cluster_id=cluster_id)
    except AcceleratorError as error:
        raise _http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error
    except Exception as error:
        logger.exception("Unable to query accelerator observability")
        raise HTTPException(
            status_code=502,
            detail={"code": "ACCELERATOR_QUERY_FAILED", "message": str(error), "retryable": True},
        ) from error


@router.post(
    "/{accelerator}/installations/preflight",
    response_model=AcceleratorPreflightResponse,
    summary="Run accelerator observability installation preflight checks without mutating the cluster.",
    description=(
        "Run accelerator observability installation preflight checks without mutating the cluster. "
        "Use this before starting a real install."
    ),
    operation_id="preflight_accelerator_install",
)
async def installation_preflight(
    accelerator: str,
    request: AcceleratorInstallRequest,
    cluster_id: str | None = Query(default=None, max_length=64),
) -> AcceleratorPreflightResponse:
    try:
        if request.accelerator != accelerator:
            raise AcceleratorError(
                "ACCELERATOR_MISMATCH",
                "Request accelerator does not match the path accelerator",
                status_code=422,
            )
        return await preflight(request, cluster_id=cluster_id)
    except AcceleratorError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error


@router.post(
    "/{accelerator}/installations",
    response_model=AcceleratorOperationResponse,
    status_code=202,
    summary="Install accelerator observability components for a supported accelerator family.",
    description="Install accelerator observability components for a supported accelerator family.",
    operation_id="install_accelerator_monitoring",
)
async def create_installation(
    accelerator: str,
    request: AcceleratorInstallRequest,
    idempotency_key: str | None = Header(default=None, max_length=128),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> AcceleratorOperationResponse:
    try:
        if request.accelerator != accelerator:
            raise AcceleratorError(
                "ACCELERATOR_MISMATCH",
                "Request accelerator does not match the path accelerator",
                status_code=422,
            )
        return await install(request, idempotency_key=idempotency_key, cluster_id=cluster_id)
    except AcceleratorError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error


@router.get(
    "/{accelerator}/operations/{operation_id}",
    response_model=AcceleratorOperationResponse,
    summary="Get the status and logs of one accelerator monitoring installation operation.",
    description="Get the status and logs of one accelerator monitoring installation operation.",
    operation_id="get_accelerator_monitoring_operation",
)
async def operation(accelerator: str, operation_id: str) -> AcceleratorOperationResponse:
    del accelerator  # operations are keyed by operation_id; accelerator is validated for routing consistency
    try:
        return get_operation(operation_id)
    except AcceleratorError as error:
        raise _http_error(error) from error


@router.get(
    "/{accelerator}/links",
    response_model=AcceleratorLinksResponse,
    summary=(
        "Get observability links Lens can resolve for an accelerator monitoring installation, such as Grafana or "
        "Prometheus URLs."
    ),
    description=(
        "Get observability links Lens can resolve for an accelerator monitoring installation, such as Grafana or "
        "Prometheus URLs."
    ),
    operation_id="get_accelerator_monitoring_links",
)
async def links(
    accelerator: str,
    cluster_id: str | None = Query(default=None, max_length=64),
) -> AcceleratorLinksResponse:
    try:
        return await get_links(accelerator, cluster_id=cluster_id)
    except AcceleratorError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail={"code": "CLUSTER_NOT_FOUND", "message": str(error)}) from error
    except Exception as error:
        logger.exception("Unable to resolve accelerator observability links")
        raise HTTPException(
            status_code=502,
            detail={"code": "ACCELERATOR_QUERY_FAILED", "message": str(error), "retryable": True},
        ) from error
