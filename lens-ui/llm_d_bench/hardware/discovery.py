"""Discover hardware profiles and providers.

Two sources, one path:

* the distribution's bundled ``profiles/*.json`` (official built-ins), and
* the ``llm_d_bench.hardware.profiles`` / ``llm_d_bench.hardware.providers``
  entry-point groups declared by this or a third-party package.

Payloads are validated against ``schema.json`` before registration; duplicate
ids with identical content are deduplicated, inconsistent duplicates raise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import metadata
from importlib.resources import files
from typing import Any

import jsonschema

from .errors import HardwareProfileInvalidError
from .models import HardwareProfile
from .registry import HardwareProvider, register_profile, register_provider

PROFILES_ENTRY_POINT_GROUP = "llm_d_bench.hardware.profiles"
PROVIDERS_ENTRY_POINT_GROUP = "llm_d_bench.hardware.providers"


def _schema() -> dict[str, Any]:
    return json.loads(files("llm_d_bench.hardware").joinpath("schema.json").read_text(encoding="utf-8"))


def validate_profile_payload(payload: Mapping[str, Any]) -> None:
    """Validate one profile payload against the shipped JSON Schema."""
    document = dict(payload)
    try:
        jsonschema.validate(instance=document, schema=_schema())
    except jsonschema.ValidationError as error:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise HardwareProfileInvalidError(f"invalid hardware profile at {location}: {error.message}") from error


def bundled_profile_payloads() -> list[dict[str, Any]]:
    """Load the JSON profiles shipped inside this package."""
    directory = files("llm_d_bench.hardware").joinpath("profiles")
    payloads: list[dict[str, Any]] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".json"):
            payloads.append(json.loads(entry.read_text(encoding="utf-8")))
    return payloads


def _coerce_payloads(value: Any, source: str) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        payloads: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise HardwareProfileInvalidError(f"{source} produced a non-object profile")
            payloads.append(dict(item))
        return payloads
    raise HardwareProfileInvalidError(f"{source} did not return a profile object or list")


def entry_point_profile_payloads() -> list[dict[str, Any]]:
    """Load profile payloads from the ``...hardware.profiles`` entry points."""
    payloads: list[dict[str, Any]] = []
    for entry_point in metadata.entry_points(group=PROFILES_ENTRY_POINT_GROUP):
        loader = entry_point.load()
        if not callable(loader):
            raise HardwareProfileInvalidError(f"profile entry point {entry_point.name!r} is not callable")
        payloads.extend(_coerce_payloads(loader(), f"profile entry point {entry_point.name!r}"))
    return payloads


def _entry_point_providers() -> list[HardwareProvider]:
    providers: list[HardwareProvider] = []
    for entry_point in metadata.entry_points(group=PROVIDERS_ENTRY_POINT_GROUP):
        loaded = entry_point.load()
        provider = loaded() if isinstance(loaded, type) else loaded
        if not isinstance(provider, HardwareProvider):
            raise HardwareProfileInvalidError(f"provider entry point {entry_point.name!r} is not a HardwareProvider")
        providers.append(provider)
    return providers


def discover_profiles() -> list[HardwareProfile]:
    """Return validated profiles from bundled data plus entry points."""
    by_id: dict[str, dict[str, Any]] = {}
    for payload in bundled_profile_payloads() + entry_point_profile_payloads():
        validate_profile_payload(payload)
        profile_id = str(payload["id"])
        existing = by_id.get(profile_id)
        if existing is not None and existing != payload:
            raise HardwareProfileInvalidError(f"hardware profile {profile_id!r} is defined inconsistently")
        by_id[profile_id] = payload
    return [HardwareProfile.from_dict(payload) for payload in by_id.values()]


def discover_and_register() -> None:
    """Register every discovered profile, then every discovered provider."""
    for profile in discover_profiles():
        register_profile(profile)
    for provider in _entry_point_providers():
        register_provider(provider)
