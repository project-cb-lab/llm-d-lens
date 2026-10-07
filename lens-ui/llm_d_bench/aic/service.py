# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""In-process adapter around the pip-installed AIConfigurator nightly SDK."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import yaml

from .models import (
    AICEstimateRequest,
    AICExperimentRequest,
    AICExperimentResponse,
    AICRequest,
    AICSearchResponse,
    AICSupportResponse,
)


class AICError(RuntimeError):
    """A user-visible AIConfigurator integration failure."""


def _cli():
    try:
        from aiconfigurator.cli import cli_default, cli_exp, cli_support
    except ImportError as error:
        raise AICError("AIConfigurator nightly is not installed; install aiconfigurator==0.11.0.dev20260728") from error
    return cli_default, cli_exp, cli_support


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _support_flags(result: Any) -> tuple[bool, bool]:
    agg = getattr(result, "agg_supported", None)
    disagg = getattr(result, "disagg_supported", None)
    if agg is not None or disagg is not None:
        return bool(agg), bool(disagg)
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), bool(result[1])
    raise AICError("AIConfigurator returned an unsupported capability result")


def check_support_sync(request: AICRequest) -> AICSupportResponse:
    _cli_default, _cli_exp, cli_support = _cli()
    try:
        result = cli_support(
            model_path=request.model_name,
            system=request.aic_system_name,
            backend=request.aic_backend_name.lower(),
        )
        agg_supported, disagg_supported = _support_flags(result)
    except Exception as error:
        raise AICError(str(error)) from error
    supported = agg_supported or disagg_supported
    return AICSupportResponse(
        supported=supported,
        agg_supported=agg_supported,
        disagg_supported=disagg_supported,
        reason=None
        if supported
        else (f"{request.model_name} is not supported on {request.aic_system_name} with {request.aic_backend_name}"),
        constraints={"gpu_count": request.gpu_count},
    )


async def check_support(request: AICRequest) -> AICSupportResponse:
    return await asyncio.to_thread(check_support_sync, request)


def _candidate(mode: str, row: dict[str, Any]) -> dict[str, Any]:
    if mode == "agg":
        tp = int(row.get("tp") or 1)
        replicas = max(1, int(row.get("num_total_gpus") or tp) // max(tp, 1))
        return {
            "mode": "agg",
            "tp": tp,
            "replicas": replicas,
            "num_total_gpus": int(row.get("num_total_gpus") or tp * replicas),
            "ttft_ms": row.get("ttft"),
            "tpot_ms": row.get("tpot"),
            "throughput_tokens_per_sec": row.get("tokens/s"),
        }
    prefill_tp = int(row.get("(p)tp") or 1)
    decode_tp = int(row.get("(d)tp") or 1)
    return {
        "mode": "disagg",
        "prefill_tp": prefill_tp,
        "prefill_replicas": int(row.get("(p)workers") or 1),
        "decode_tp": decode_tp,
        "decode_replicas": int(row.get("(d)workers") or 1),
        "num_total_gpus": int(row.get("num_total_gpus") or 1),
        "ttft_ms": row.get("ttft"),
        "tpot_ms": row.get("tpot"),
        "throughput_tokens_per_sec": row.get("tokens/s"),
    }


def _result_candidates(result: Any, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for mode, frame in result.best_configs.items():
        normalized_mode = "agg" if mode == "agg" else "disagg"
        records = frame.head(limit).to_dict(orient="records")
        candidates.extend(_plain(_candidate(normalized_mode, row)) for row in records)
    return candidates


def search_sync(request: AICRequest) -> AICSearchResponse:
    cli_default, _cli_exp, _cli_support = _cli()
    kwargs: dict[str, Any] = {
        "model_path": request.model_name,
        "total_gpus": request.gpu_count,
        "system": request.aic_system_name,
        "backend": request.aic_backend_name.lower(),
        "database_mode": request.aic_database_mode.upper(),
        "isl": request.mean_input_tokens,
        "osl": request.mean_output_tokens,
        "top_n": request.max_candidates,
    }
    if request.ttft_target_ms is not None:
        kwargs["ttft"] = request.ttft_target_ms
    if request.tpot_target_ms is not None:
        kwargs["tpot"] = request.tpot_target_ms
    try:
        result = cli_default(**kwargs)
    except Exception as error:
        raise AICError(str(error)) from error
    return AICSearchResponse(
        configs=_result_candidates(result, request.max_candidates),
        chosen_mode=result.chosen_exp,
    )


async def search(request: AICRequest) -> AICSearchResponse:
    return await asyncio.to_thread(search_sync, request)


def experiments_sync(request: AICExperimentRequest) -> AICExperimentResponse:
    _cli_default, cli_exp, _cli_support = _cli()
    try:
        parsed = yaml.safe_load(request.yaml_text)
        if not isinstance(parsed, dict):
            raise AICError("AIConfigurator experiment YAML must contain an object")
        result = cli_exp(config=parsed, top_n=request.top_n)
    except AICError:
        raise
    except Exception as error:
        raise AICError(str(error)) from error
    experiments: list[dict[str, Any]] = []
    for mode, frame in result.best_configs.items():
        for index, row in enumerate(frame.to_dict(orient="records")):
            item = _candidate("agg" if mode == "agg" else "disagg", row)
            item.update({"name": f"{mode}-{index + 1}", "is_chosen": mode == result.chosen_exp})
            experiments.append(_plain(item))
    return AICExperimentResponse(experiments=experiments)


async def experiments(request: AICExperimentRequest) -> AICExperimentResponse:
    return await asyncio.to_thread(experiments_sync, request)


def estimate_sync(payload: AICEstimateRequest) -> dict[str, Any]:
    """Return an estimate only when AIC's experiment matches the requested topology."""
    is_agg = payload.scenario == "inference_scheduling"
    if is_agg and payload.pp != 1:
        raise AICError("Aggregated estimate cannot verify PP greater than 1 in AIC's response")
    required_gpus = (
        payload.tp * payload.pp * payload.replicas
        if is_agg
        else payload.prefill_tp * payload.prefill_replicas + payload.decode_tp * payload.decode_replicas
    )
    if payload.gpu_count != required_gpus:
        raise AICError(f"Requested topology requires {required_gpus} GPUs, not {payload.gpu_count}")
    if payload.ttft_target_ms is None and payload.tpot_target_ms is None:
        try:
            from aiconfigurator.cli.api import cli_estimate

            kwargs: dict[str, Any] = {
                "model_path": payload.model_name,
                "system_name": payload.aic_system_name,
                "backend_name": payload.aic_backend_name.lower(),
                "database_mode": payload.aic_database_mode.upper(),
                "mode": "agg" if is_agg else "disagg",
                "isl": payload.mean_input_tokens,
                "osl": payload.mean_output_tokens,
            }
            if is_agg:
                kwargs.update(tp_size=payload.tp, pp_size=payload.pp, batch_size=1)
            else:
                kwargs.update(
                    prefill_tp_size=payload.prefill_tp,
                    prefill_num_workers=payload.prefill_replicas,
                    prefill_batch_size=1,
                    decode_tp_size=payload.decode_tp,
                    decode_num_workers=payload.decode_replicas,
                    decode_batch_size=1,
                )
            result = cli_estimate(**kwargs)
            raw = result.raw
            if is_agg:
                topology_matches = (
                    raw.get("tp") == payload.tp
                    and raw.get("pp") == payload.pp
                    and raw.get("num_total_gpus") == payload.tp * payload.pp
                )
            else:
                topology_matches = (
                    raw.get("(p)tp") == payload.prefill_tp
                    and raw.get("(p)workers") == payload.prefill_replicas
                    and raw.get("(d)tp") == payload.decode_tp
                    and raw.get("(d)workers") == payload.decode_replicas
                    and raw.get("num_total_gpus") == required_gpus
                )
            if not topology_matches or not result.ttft or not result.tpot:
                raise AICError("AIConfigurator returned no estimate matching the requested topology")
            prediction = {
                "mode": "agg" if is_agg else "disagg",
                "num_total_gpus": required_gpus,
                "ttft_ms": result.ttft,
                "tpot_ms": result.tpot,
                "throughput_tokens_per_sec": raw.get("tokens/s") if not is_agg or payload.replicas == 1 else None,
            }
            if is_agg:
                prediction.update(tp=payload.tp, replicas=payload.replicas)
            else:
                prediction.update(
                    prefill_tp=payload.prefill_tp,
                    prefill_replicas=payload.prefill_replicas,
                    decode_tp=payload.decode_tp,
                    decode_replicas=payload.decode_replicas,
                )
            return prediction
        except AICError:
            raise
        except Exception as error:
            raise AICError(str(error)) from error
    experiment: dict[str, Any] = {
        "serving_mode": "agg" if is_agg else "disagg",
        "isl": payload.mean_input_tokens,
        "osl": payload.mean_output_tokens,
        "ttft": payload.ttft_target_ms or 2000,
        "tpot": payload.tpot_target_ms or 30,
        "total_gpus": payload.gpu_count,
        "database_mode": payload.aic_database_mode.upper(),
    }
    model = payload.model_name
    system = payload.aic_system_name
    backend = payload.aic_backend_name.lower()
    if is_agg:
        experiment.update(
            model_path=model,
            system_name=system,
            backend_name=backend,
            agg_tp_candidates=[payload.tp],
            agg_pp_candidates=[payload.pp],
            agg_num_gpu_candidates=[payload.tp * payload.pp],
        )
    else:
        for role in ("prefill", "decode"):
            experiment[f"{role}_model_path"] = model
            experiment[f"{role}_system_name"] = system
            experiment[f"{role}_backend_name"] = backend
            experiment[f"{role}_tp_candidates"] = [getattr(payload, f"{role}_tp")]
            experiment[f"{role}_num_gpu_candidates"] = [getattr(payload, f"{role}_tp")]
    response = experiments_sync(AICExperimentRequest(yaml_text=yaml.safe_dump({"manual": experiment}), top_n=1))
    for result in response.experiments:
        if not result.get("ttft_ms") or not result.get("tpot_ms"):
            continue
        if result.get("num_total_gpus") not in (None, required_gpus):
            continue
        if (
            is_agg
            and result.get("mode") == "agg"
            and result.get("tp") == payload.tp
            and result.get("replicas") == payload.replicas
        ):
            return result
        if (
            not is_agg
            and result.get("mode") == "disagg"
            and all(
                result.get(field) == getattr(payload, field)
                for field in ("prefill_tp", "prefill_replicas", "decode_tp", "decode_replicas")
            )
        ):
            return result
    raise AICError("AIConfigurator returned no estimate matching the requested topology")


async def estimate(payload: AICEstimateRequest) -> dict[str, Any]:
    return await asyncio.to_thread(estimate_sync, payload)
