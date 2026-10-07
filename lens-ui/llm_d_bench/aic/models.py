# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""HTTP contracts for the in-process AIConfigurator adapter."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AICRequest(BaseModel):
    model_name: str = Field(min_length=1)
    gpu_count: int = Field(default=8, ge=1, le=128)
    mean_input_tokens: int = Field(default=1024, ge=1)
    mean_output_tokens: int = Field(default=256, ge=1)
    ttft_target_ms: float | None = None
    tpot_target_ms: float | None = None
    aic_system_name: str = Field(default="b60", min_length=1)
    aic_backend_name: str = Field(default="vllm", min_length=1)
    aic_database_mode: str = Field(default="SILICON", min_length=1)
    max_candidates: int = Field(default=10, ge=1, le=50)


class AICExperimentRequest(BaseModel):
    yaml_text: str = Field(min_length=1)
    top_n: int = Field(default=10, ge=1, le=50)


class AICEstimateRequest(BaseModel):
    model_name: str = Field(min_length=1)
    scenario: Literal["inference_scheduling", "pd_disaggregation"]
    gpu_count: int = Field(ge=1)
    mean_input_tokens: int = Field(default=4096, ge=1)
    mean_output_tokens: int = Field(default=256, ge=1)
    ttft_target_ms: float | None = Field(default=None, gt=0)
    tpot_target_ms: float | None = Field(default=None, gt=0)
    aic_system_name: str = Field(default="b60", min_length=1)
    aic_backend_name: str = Field(default="vllm", min_length=1)
    aic_database_mode: str = Field(default="SILICON", min_length=1)
    tp: int = Field(default=1, ge=1)
    pp: int = Field(default=1, ge=1)
    replicas: int = Field(default=1, ge=1)
    prefill_tp: int = Field(default=1, ge=1)
    prefill_replicas: int = Field(default=1, ge=1)
    decode_tp: int = Field(default=1, ge=1)
    decode_replicas: int = Field(default=1, ge=1)


class AICSupportResponse(BaseModel):
    supported: bool
    agg_supported: bool
    disagg_supported: bool
    epd_supported: bool = False
    reason: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)


class AICSearchResponse(BaseModel):
    configs: list[dict[str, Any]] = Field(default_factory=list)
    chosen_mode: str | None = None


class AICExperimentResponse(BaseModel):
    experiments: list[dict[str, Any]] = Field(default_factory=list)
