# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""AIConfigurator capability check used internally by resolve."""

from typing import Any

from llm_d_bench.aic.models import AICRequest
from llm_d_bench.aic.service import AICError, check_support

from .models import CandidateConfig


async def check_capability(candidate: CandidateConfig) -> tuple[bool, str | None, list[str]]:
    """Return support status, failure reason, and non-fatal warnings."""
    model = candidate.target.get("model")
    hardware: dict[str, Any] = candidate.target.get("hardware") or {}
    system_name = hardware.get("aic_system_name") or hardware.get("accelerator_model")
    if not model or not system_name:
        return True, None, ["Capability check skipped because model or accelerator model is missing"]

    accelerator_count = int(hardware.get("accelerator_count") or hardware.get("gpu_count") or 1)
    backend = hardware.get("aic_backend_name") or hardware.get("backend") or "vllm"
    try:
        result = await check_support(
            AICRequest(
                model_name=str(model),
                gpu_count=accelerator_count,
                aic_system_name=str(system_name),
                aic_backend_name=str(backend).lower(),
            )
        )
    except (AICError, ValueError) as error:
        return False, f"AIConfigurator capability check failed: {error}", []

    if not result.supported:
        reason = result.reason or f"{model} is not supported on {system_name}"
        return False, str(reason), []
    mode_supported = {
        "baseline": result.agg_supported,
        "pd": result.disagg_supported,
        "epd": result.epd_supported,
        "tiered_cache": result.agg_supported,
    }.get(candidate.type)
    if mode_supported is False:
        return False, f"{candidate.type} topology is not supported for {model} on {system_name}", []
    constraints = result.constraints
    warnings = [f"AIConfigurator constraints: {constraints}"] if constraints else []
    return True, None, warnings
