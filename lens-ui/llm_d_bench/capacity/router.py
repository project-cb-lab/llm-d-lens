"""REST API router for capacity planning and vLLM inference resource estimation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from .validator import CapacityEvaluationResult, ValidationParams, evaluate_capacity

router = APIRouter(prefix="/api/v1/capacity", tags=["capacity-planner"])


class CapacityEstimateRequest(BaseModel):
    model: str = Field(..., description="HuggingFace model ID or local model path (e.g. meta-llama/Llama-3-8B)")
    gpu_memory_gib: float = Field(..., gt=0, description="Per-device GPU VRAM in GiB (e.g. 80 for H100/A100)")
    tensor_parallel_size: int = Field(default=1, ge=1, description="Tensor parallelism degree")
    max_model_len: int = Field(default=4096, ge=256, description="Maximum sequence context length in tokens")
    gpu_memory_utilization: float = Field(default=0.9, gt=0, le=1, description="GPU memory utilization factor")
    pipeline_parallel_size: int = Field(default=1, ge=1, description="Pipeline parallelism degree")
    replicas: int = Field(default=1, ge=1, description="Number of model replicas")
    model_weight_gib: float | None = Field(default=None, gt=0, description="Optional known model weight in GiB")


@router.post(
    "/estimate",
    response_model=CapacityEvaluationResult,
    status_code=status.HTTP_200_OK,
    summary="Estimate vLLM capacity and memory breakdown",
    description="Calculates allocatable KV cache, peak activations, valid TP, and maximum concurrency.",
)
async def estimate_capacity_endpoint(request: CapacityEstimateRequest) -> CapacityEvaluationResult:
    params = ValidationParams(
        models=[request.model],
        gpu_memory=request.gpu_memory_gib,
        tp=request.tensor_parallel_size,
        pp=request.pipeline_parallel_size,
        dp=1,
        gpu_memory_util=request.gpu_memory_utilization,
        max_model_len=request.max_model_len,
        replicas=request.replicas,
        fallback_weight_gib=request.model_weight_gib,
    )
    return evaluate_capacity(params)
