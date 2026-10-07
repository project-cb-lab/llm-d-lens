"""FastAPI routes exposing registered hardware profiles."""

from __future__ import annotations

from fastapi import APIRouter

from .models import HardwareCapabilitiesResponse
from .service import capabilities

router = APIRouter(prefix="/api/v1/hardware", tags=["hardware"])


@router.get(
    "/capabilities",
    response_model=HardwareCapabilitiesResponse,
    summary="List the hardware profiles Lens can resolve.",
    description=(
        "Return every registered hardware profile, built-in and plugin, with a "
        "content-hash version so gateway and browser clients can cache safely."
    ),
    operation_id="list_hardware_capabilities",
)
async def list_capabilities() -> HardwareCapabilitiesResponse:
    return capabilities()
