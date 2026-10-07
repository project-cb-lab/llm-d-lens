"""Hardware provider plugin mechanism.

Profiles are declarative JSON validated against ``schema.json``; providers are
optional Python code. Discovery uses the ``llm_d_bench.hardware.profiles`` and
``llm_d_bench.hardware.providers`` entry-point groups plus the distribution's
bundled profiles. See ``docs/design/hardware-plugin-architecture.md``.
"""

from .models import HardwareProfile
from .registry import (
    HardwareProvider,
    all_profiles,
    get_profile,
    get_provider,
    register_profile,
    register_provider,
)
from .resolver import (
    require_accelerator,
    resolve_by_accelerator_key,
    resolve_by_aic_system,
    resolve_by_device_class,
    resolve_by_node_label,
    resolve_by_resource,
    resolve_by_upstream_variant,
)
from .router import router

__all__ = [
    "HardwareProfile",
    "HardwareProvider",
    "all_profiles",
    "get_profile",
    "get_provider",
    "register_profile",
    "register_provider",
    "require_accelerator",
    "resolve_by_accelerator_key",
    "resolve_by_aic_system",
    "resolve_by_device_class",
    "resolve_by_node_label",
    "resolve_by_resource",
    "resolve_by_upstream_variant",
    "router",
]
