"""FastAPI routes for installing the Intel GPU DRA driver / device plugin."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from .errors import GpuDriverError
from .models import (
    GpuAccessMode,
    GpuDriverInstallRequest,
    GpuDriverInstallResponse,
    GpuDriverStatusResponse,
)
from .service import get_status, install

router = APIRouter(prefix="/api/v1/monitoring/gpu-driver", tags=["gpu-driver"])


def _http_error(error: GpuDriverError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get(
    "/status",
    response_model=GpuDriverStatusResponse,
    summary="Get the current GPU driver installation status for a cluster, including readiness and access mode.",
    description="Get the current GPU driver installation status for a cluster, including readiness and access mode.",
    operation_id="get_gpu_driver_status",
)
async def read_status(
    access_mode: Annotated[GpuAccessMode, Query()] = "dra",
    cluster_id: Annotated[str | None, Query(max_length=64)] = None,
    hardware: Annotated[str | None, Query(max_length=64)] = None,
) -> GpuDriverStatusResponse:
    try:
        return await get_status(access_mode, cluster_id=cluster_id, hardware=hardware)
    except GpuDriverError as error:
        raise _http_error(error) from error


@router.post(
    "/install",
    response_model=GpuDriverInstallResponse,
    status_code=202,
    summary="Install the GPU driver/device-plugin stack Lens expects for the selected access mode.",
    description="Install the GPU driver/device-plugin stack Lens expects for the selected access mode.",
    operation_id="install_gpu_driver",
)
async def create_install(
    payload: GpuDriverInstallRequest,
    cluster_id: str | None = Query(default=None, max_length=64),
    hardware: str | None = Query(default=None, max_length=64),
) -> GpuDriverInstallResponse:
    try:
        return await install(payload.access_mode, cluster_id=cluster_id, hardware=hardware)
    except GpuDriverError as error:
        raise _http_error(error) from error
