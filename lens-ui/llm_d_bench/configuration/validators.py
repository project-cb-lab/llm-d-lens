# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""CandidateConfig structural and resource validation."""

from .managed_environment import admin_managed_environment_error, admin_managed_environment_names
from .models import CandidateConfig, ValidationResult


def _component_devices(component: dict | None, name: str, errors: list[str]) -> int:
    if not component:
        errors.append(f"{name} configuration is required")
        return 0
    tp = component.get("tensor_parallel_size")
    replicas = component.get("replicas")
    if not isinstance(tp, int) or tp < 1:
        errors.append(f"{name}.tensor_parallel_size must be a positive integer")
    if not isinstance(replicas, int) or replicas < 1:
        errors.append(f"{name}.replicas must be a positive integer")
    return tp * replicas if isinstance(tp, int) and tp > 0 and isinstance(replicas, int) and replicas > 0 else 0


def validate_candidate(candidate: CandidateConfig, capability_warnings: list[str] | None = None) -> ValidationResult:
    """Validate topology requirements and accelerator budget."""
    errors: list[str] = []
    warnings = list(capability_warnings or [])
    devices = 0

    if not candidate.target.get("model"):
        warnings.append("Model is not present; capability verification was skipped")

    if candidate.type == "baseline":
        devices += _component_devices(candidate.serving, "serving", errors)
    elif candidate.type == "pd":
        devices += _component_devices(candidate.prefill, "prefill", errors)
        devices += _component_devices(candidate.decode, "decode", errors)
    elif candidate.type == "epd":
        devices += _component_devices(candidate.encode, "encode", errors)
        devices += _component_devices(candidate.prefill, "prefill", errors)
        devices += _component_devices(candidate.decode, "decode", errors)
    elif candidate.type == "tiered_cache":
        devices += _component_devices(candidate.serving, "serving", errors)
        if not candidate.cache:
            errors.append("cache configuration is required for tiered_cache")

    hardware = candidate.target.get("hardware") or {}
    available = hardware.get("accelerator_count") or hardware.get("gpu_count")
    if available is not None:
        try:
            available_count = int(available)
            if devices > available_count:
                errors.append(f"Topology requires {devices} accelerators but only {available_count} are available")
        except (TypeError, ValueError):
            errors.append("hardware.accelerator_count must be an integer")
    elif devices:
        warnings.append(f"Accelerator budget is not specified; topology requires {devices}")

    for name in admin_managed_environment_names(getattr(candidate, "custom_parameters", None)):
        errors.append(admin_managed_environment_error(name))

    if candidate.network:
        inference_pool = candidate.network.get("inference_pool")
        if not isinstance(inference_pool, dict):
            errors.append("network.inference_pool is required")
        else:
            for field in ("target_port_number",):
                value = inference_pool.get(field)
                if not isinstance(value, int) or value < 1 or value > 65535:
                    errors.append(f"network.inference_pool.{field} must be a valid TCP port")
            extension_ref = inference_pool.get("extension_ref")
            if not isinstance(extension_ref, dict):
                errors.append("network.inference_pool.extension_ref is required")
            else:
                extension_port = extension_ref.get("port_number")
                if not isinstance(extension_port, int) or extension_port < 1 or extension_port > 65535:
                    errors.append("network.inference_pool.extension_ref.port_number must be a valid TCP port")

    status = "invalid" if errors else "valid_with_warnings" if warnings else "valid"
    return ValidationResult(status=status, errors=errors, warnings=warnings)
