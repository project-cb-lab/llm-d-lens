# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Local HTTP API backed by the pip-installed AIConfigurator nightly SDK."""

from fastapi import APIRouter, HTTPException

from .models import AICEstimateRequest, AICExperimentRequest, AICRequest
from .service import AICError, check_support, estimate, experiments, search

router = APIRouter(prefix="/api/v1/aic", tags=["aic"])


def _bad_request(error: AICError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@router.post(
    "/support",
    summary="Check AIConfigurator support for a model and hardware target.",
    description=(
        "Check whether AIConfigurator supports the requested model, system, backend, and database mode. Returns "
        "supported serving modes, constraints, and a reason when unsupported. Use this before candidate search."
    ),
    operation_id="check_aic_support",
)
async def support_endpoint(request: AICRequest):
    try:
        return await check_support(request)
    except AICError as error:
        raise _bad_request(error) from error


@router.post(
    "/search",
    summary="Search AIConfigurator for serving configuration candidates.",
    description=(
        "Search for serving configurations using the requested model, hardware target, workload token lengths, and "
        "latency targets. Returns candidate configurations and the chosen serving mode, subject to max_candidates."
    ),
    operation_id="search_aic_configurations",
)
async def search_endpoint(request: AICRequest):
    try:
        return await search(request)
    except AICError as error:
        raise _bad_request(error) from error


@router.post(
    "/experiments",
    summary=("Parse an AIConfigurator experiment YAML file and return the top experiments Lens can extract from it."),
    description="Parse an AIConfigurator experiment YAML file and return the top experiments Lens can extract from it.",
    operation_id="list_aic_experiments",
)
async def experiments_endpoint(request: AICExperimentRequest):
    try:
        return await experiments(request)
    except AICError as error:
        raise _bad_request(error) from error


@router.post(
    "/estimate",
    summary="Estimate one exact AIConfigurator serving topology.",
    description=(
        "Estimate TTFT and TPOT for an aggregated or P/D topology with explicit tensor parallelism, replicas and "
        "total GPU count. Reject results that do not match the requested topology."
    ),
    operation_id="estimate_aic_configuration",
)
async def estimate_endpoint(payload: AICEstimateRequest):
    try:
        return await estimate(payload)
    except AICError as error:
        raise _bad_request(error) from error
