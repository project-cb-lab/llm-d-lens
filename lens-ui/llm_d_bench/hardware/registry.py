"""In-process registry of hardware profiles and providers.

Profiles are registered once (built-ins plus entry-point plugins) and indexed
by the identities other layers resolve against: device class, extended-resource
prefix, upstream variant and accelerator key. Registration is fail-fast: two
profiles claiming the same identity raise instead of silently shadowing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .errors import HardwareProfileNotFoundError, HardwareRegistrationError
from .models import HardwareProfile

_PROFILES: dict[str, HardwareProfile] = {}
_PROVIDERS: dict[str, HardwareProvider] = {}
_DEVICE_CLASS_INDEX: dict[str, str] = {}
_RESOURCE_INDEX: dict[str, str] = {}
_VARIANT_INDEX: dict[str, str] = {}
_KEY_INDEX: dict[str, str] = {}
_loaded = False


@runtime_checkable
class HardwareProvider(Protocol):
    """Code contribution for a hardware profile: driver install and presence."""

    def profile(self) -> HardwareProfile: ...

    async def ensure_presence(self, *, cluster_id: str | None) -> None: ...

    async def driver_status(self, access_mode: str, *, cluster_id: str | None) -> Any: ...

    async def install_driver(self, access_mode: str, *, cluster_id: str | None) -> Any: ...


def _index_conflict(index: dict[str, str], key: str, profile_id: str, field: str) -> str | None:
    owner = index.get(key)
    if owner is not None and owner != profile_id:
        return f"{field} {key!r} is already claimed by hardware profile {owner!r}"
    return None


def _collect_conflicts(profile: HardwareProfile) -> list[str]:
    conflicts: list[str] = []
    for device_class in profile.device_classes:
        conflict = _index_conflict(_DEVICE_CLASS_INDEX, device_class, profile.id, "device class")
        if conflict:
            conflicts.append(conflict)
    for prefix in profile.resource_prefixes:
        conflict = _index_conflict(_RESOURCE_INDEX, prefix, profile.id, "resource prefix")
        if conflict:
            conflicts.append(conflict)
    for alias in profile.accelerator_aliases:
        conflict = _index_conflict(_KEY_INDEX, alias, profile.id, "accelerator key")
        if conflict:
            conflicts.append(conflict)
    if profile.upstream_variant:
        conflict = _index_conflict(_VARIANT_INDEX, profile.upstream_variant, profile.id, "upstream variant")
        if conflict:
            conflicts.append(conflict)
    return conflicts


def _index_profile(profile: HardwareProfile) -> None:
    for device_class in profile.device_classes:
        _DEVICE_CLASS_INDEX[device_class] = profile.id
    for prefix in profile.resource_prefixes:
        _RESOURCE_INDEX[prefix] = profile.id
    for alias in profile.accelerator_aliases:
        _KEY_INDEX[alias] = profile.id
    if profile.upstream_variant:
        _VARIANT_INDEX[profile.upstream_variant] = profile.id


def _deindex_profile(profile: HardwareProfile) -> None:
    for index, keys in (
        (_DEVICE_CLASS_INDEX, profile.device_classes),
        (_RESOURCE_INDEX, profile.resource_prefixes),
        (_KEY_INDEX, tuple(profile.accelerator_aliases)),
    ):
        for key in keys:
            if index.get(key) == profile.id:
                del index[key]
    if profile.upstream_variant and _VARIANT_INDEX.get(profile.upstream_variant) == profile.id:
        del _VARIANT_INDEX[profile.upstream_variant]


def register_profile(profile: HardwareProfile, *, replace: bool = False) -> None:
    """Register one profile, rejecting identity conflicts with other profiles."""
    existing = _PROFILES.get(profile.id)
    if existing is not None and not replace:
        raise HardwareRegistrationError(f"hardware profile {profile.id!r} is already registered")
    conflicts = _collect_conflicts(profile)
    if conflicts:
        raise HardwareRegistrationError("; ".join(conflicts))
    if existing is not None:
        _deindex_profile(existing)
    _PROFILES[profile.id] = profile
    _index_profile(profile)


def register_provider(provider: HardwareProvider) -> None:
    """Register one provider under the id of the profile it exposes."""
    profile_id = provider.profile().id
    if profile_id in _PROVIDERS:
        raise HardwareRegistrationError(f"hardware provider {profile_id!r} is already registered")
    _PROVIDERS[profile_id] = provider


def all_profiles() -> list[HardwareProfile]:
    ensure_loaded()
    return list(_PROFILES.values())


def get_profile(profile_id: str) -> HardwareProfile:
    ensure_loaded()
    profile = _PROFILES.get(profile_id)
    if profile is None:
        raise HardwareProfileNotFoundError(f"hardware profile {profile_id!r} is not registered")
    return profile


def get_provider(profile_id: str) -> HardwareProvider | None:
    ensure_loaded()
    return _PROVIDERS.get(profile_id)


def get_driver_provider(profile_id: str | None = None) -> HardwareProvider:
    """Return a provider that declares driver install support.

    With ``profile_id`` (the gpu-driver route's optional ``hardware`` query
    parameter) the exact provider is selected. Without it, exactly one
    registered provider may declare a driver; a second one raises because the
    caller must then pass ``hardware`` explicitly.
    """
    ensure_loaded()
    if profile_id:
        provider = _PROVIDERS.get(profile_id)
        if provider is None or provider.profile().driver is None:
            raise HardwareProfileNotFoundError(
                f"hardware profile {profile_id!r} is not registered with driver support"
            )
        return provider
    providers = [provider for provider in _PROVIDERS.values() if provider.profile().driver is not None]
    if not providers:
        raise HardwareProfileNotFoundError("no registered hardware provider declares a driver")
    if len(providers) > 1:
        raise HardwareRegistrationError(
            "multiple hardware providers declare a driver; pass the hardware query parameter to select one"
        )
    return providers[0]


def ensure_loaded() -> None:
    """Load bundled and entry-point profiles/providers exactly once."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import discovery

    discovery.discover_and_register()


def _reset_for_tests() -> None:
    """Clear all registry state so tests can register in isolation."""
    global _loaded
    _PROFILES.clear()
    _PROVIDERS.clear()
    _DEVICE_CLASS_INDEX.clear()
    _RESOURCE_INDEX.clear()
    _VARIANT_INDEX.clear()
    _KEY_INDEX.clear()
    _loaded = False
