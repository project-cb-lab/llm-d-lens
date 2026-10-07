"""Resolve hardware profiles by the identity each layer actually has.

Core code asks these functions instead of branching on vendor names: a DRA
device class, an extended-resource key, a node label, an upstream variant, or
an accelerator key. Explicit use of an unregistered identity raises rather
than silently guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from .errors import HardwareProfileNotFoundError
from .models import HardwareProfile
from .registry import all_profiles


def _first(predicate: Callable[[HardwareProfile], bool]) -> HardwareProfile | None:
    for profile in all_profiles():
        if predicate(profile):
            return profile
    return None


def resolve_by_device_class(device_class: str) -> HardwareProfile | None:
    if not device_class:
        return None
    return _first(lambda profile: device_class in profile.device_classes)


def resolve_by_resource(resource_key: str) -> HardwareProfile | None:
    """Resolve an extended-resource key (e.g. ``nvidia.com/gpu``) by prefix."""
    if not resource_key:
        return None
    key = resource_key.strip()

    def matches(profile: HardwareProfile) -> bool:
        for prefix in profile.resource_prefixes:
            base = prefix.rstrip("/")
            if key in {prefix, base} or key.startswith(prefix) or key.startswith(f"{base}/"):
                return True
        return False

    return _first(matches)


def resolve_by_node_label(labels: Mapping[str, str]) -> HardwareProfile | None:
    if not labels:
        return None

    def matches(profile: HardwareProfile) -> bool:
        selector = profile.node_label_selector
        return bool(selector) and all(labels.get(key) == value for key, value in selector.items())

    return _first(matches)


def resolve_by_upstream_variant(variant: str, vendor: str | None = None) -> HardwareProfile | None:
    if not variant:
        return None

    def matches(profile: HardwareProfile) -> bool:
        if profile.upstream_variant != variant:
            return False
        return not (vendor and profile.upstream_vendor and profile.upstream_vendor != vendor)

    return _first(matches)


def resolve_by_accelerator_key(key: str) -> HardwareProfile | None:
    if not key:
        return None
    return _first(lambda profile: key in profile.accelerator_aliases)


def resolve_by_aic_system(system_name: str) -> HardwareProfile | None:
    if not system_name:
        return None

    def matches(profile: HardwareProfile) -> bool:
        return any(
            pattern and re.search(pattern, system_name, re.IGNORECASE)
            for pattern in profile.planning.aic_system_patterns
        )

    return _first(matches)


def require_accelerator(key: str) -> HardwareProfile:
    """Return the profile for an explicitly requested accelerator, or raise."""
    profile = resolve_by_accelerator_key(key)
    if profile is None:
        raise HardwareProfileNotFoundError(f"no registered hardware profile supports accelerator {key!r}")
    return profile
