"""Use-case helpers for the hardware profile API."""

from __future__ import annotations

from llm_d_bench.common.hashing import stable_hash

from .models import HardwareCapabilitiesResponse
from .registry import all_profiles


def capabilities() -> HardwareCapabilitiesResponse:
    """Return every registered profile plus a content-hash version."""
    profiles = [profile.to_dict() for profile in all_profiles()]
    version = stable_hash({"profiles": profiles})
    return HardwareCapabilitiesResponse(version=version, profiles=profiles)
