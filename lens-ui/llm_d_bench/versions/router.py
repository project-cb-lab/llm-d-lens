"""Read-only API for the pinned llm-d stack profile."""

from __future__ import annotations

from fastapi import APIRouter

from llm_d_bench.versions import describe

router = APIRouter(prefix="/api/v1/versions", tags=["versions"])


@router.get(
    "",
    summary="Get the pinned llm-d component versions Lens supports.",
    description=(
        "Get the pinned llm-d component versions Lens supports (llm-d, llm-d-router, "
        "llm-d-benchmark, inference payload processor, Gateway API/GIE CRDs and gateway "
        "providers). Read-only; the wizard and API always use these values."
    ),
    operation_id="get_supported_versions",
)
async def read_versions() -> dict:
    return describe()
