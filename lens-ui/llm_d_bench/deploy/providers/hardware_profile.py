"""Hardware identity for rendered deployment overlays.

These helpers read device class, claim request name and overlay variant from
the registered hardware profile instead of hardcoding them. Every function
accepts an explicit ``accelerator`` keyword: callers pass the specific run's
accelerator (the UI selection recorded on that run's configuration/provenance),
so concurrent deployments for different clusters/vendors never share state.
``PRISM_DEPLOY_ACCELERATOR``/the hardcoded Intel default are consulted only
when a caller omits ``accelerator`` (legacy/no-op-safe fallback for callers
that have no per-run value yet).
"""

from __future__ import annotations

import os

from llm_d_bench.hardware.models import HardwareProfile
from llm_d_bench.hardware.registry import all_profiles
from llm_d_bench.hardware.resolver import resolve_by_accelerator_key

_ACTIVE_ACCELERATOR_KEY = "xpu"
_ACCELERATOR_ENV = "PRISM_DEPLOY_ACCELERATOR"
_ACCELERATOR_ALIASES = {
    "xpu": "xpu",
    "intel": "intel_gpu",
    "intel-xpu": "intel_gpu",
    "intel_gpu": "intel_gpu",
    "gpu": "cuda",
    "nvidia": "cuda",
    "cuda": "cuda",
    "nvidia_gpu": "cuda",
}

DEFAULT_DEVICE_CLASS = "gpu.intel.com"
DEFAULT_CLAIM_REQUEST_NAME = "intel"
DEFAULT_OVERLAY_VARIANT = "xpu"


def _active_accelerator_key(accelerator: str | None = None) -> str:
    """Resolve the accelerator key for one render/deploy call.

    ``accelerator`` is the explicit, per-run value (from the run's own
    provenance/UI selection); it always wins. Only when nothing recorded one
    does this fall back to ``PRISM_DEPLOY_ACCELERATOR``/the Intel default, so
    legacy callers (and tests) keep working unchanged.
    """
    explicit = (accelerator or "").strip().lower()
    if explicit:
        return _ACCELERATOR_ALIASES.get(explicit, explicit)
    value = (os.environ.get(_ACCELERATOR_ENV) or "").strip().lower()
    return _ACCELERATOR_ALIASES.get(value, _ACTIVE_ACCELERATOR_KEY)


def active_profile(accelerator: str | None = None) -> HardwareProfile | None:
    """The hardware profile to render for, honoring an explicit per-run accelerator."""
    return resolve_by_accelerator_key(_active_accelerator_key(accelerator))


def device_class(fallback: str = DEFAULT_DEVICE_CLASS, *, accelerator: str | None = None) -> str:
    profile = active_profile(accelerator)
    return (profile.deployment.device_class if profile else "") or fallback


def claim_request_name(fallback: str = DEFAULT_CLAIM_REQUEST_NAME, *, accelerator: str | None = None) -> str:
    profile = active_profile(accelerator)
    return (profile.deployment.claim_request_name if profile else "") or fallback


def overlay_variant(fallback: str = DEFAULT_OVERLAY_VARIANT, *, accelerator: str | None = None) -> str:
    profile = active_profile(accelerator)
    return (profile.deployment.arch if profile else "") or fallback


def accelerator_supported(key: str | None) -> bool:
    """True when a registered hardware profile supports this accelerator key."""
    if not key:
        return False
    return resolve_by_accelerator_key(str(key)) is not None


DEFAULT_REQUEST_MODEL = "dra"


def request_model(fallback: str = DEFAULT_REQUEST_MODEL, *, accelerator: str | None = None) -> str:
    """How the profile requests accelerators: ``dra`` or ``extended-resource``."""
    profile = active_profile(accelerator)
    return (profile.request_model if profile else "") or fallback


def requires_dra_claim(*, accelerator: str | None = None) -> bool:
    return request_model(accelerator=accelerator) == "dra"


def resource_name(fallback: str | None = None, *, accelerator: str | None = None) -> str | None:
    """Extended-resource key (e.g. ``nvidia.com/gpu``) for non-DRA profiles."""
    profile = active_profile(accelerator)
    return (profile.deployment.resource_name if profile else None) or fallback


# Fallback used only before hardware discovery resolves a profile. The profile's
# ``deployment.runtime_image`` is the source of truth; each profile owns both the
# repository and the version, so a vendor/version change is data, not a code
# branch here.
DEFAULT_RUNTIME_IMAGE = "ghcr.io/llm-d/llm-d-xpu:v0.9.0"


def _image_repository(image: str) -> str:
    """Strip any tag/digest so two references to the same repository compare equal."""
    if "@" in image:
        return image.split("@", 1)[0]
    last = image.rsplit("/", 1)[-1]
    return image.rsplit(":", 1)[0] if ":" in last else image


# Model-server image families Lens manages. A stored configuration may carry an
# older llm-d image (``llm-d-cuda``/``llm-d-xpu``) or an upstream vLLM one; both are
# normalized to the active profile's ``runtime_image``. Anything else is custom and
# left untouched.
_MANAGED_MODEL_IMAGE_REPOSITORIES = {
    "ghcr.io/llm-d/llm-d-cuda",
    "ghcr.io/llm-d/llm-d-xpu",
    "ghcr.io/llm-d/llm-d-rocm",
    "docker.io/vllm/vllm-openai",
    "docker.io/vllm/vllm-openai-xpu",
    "docker.io/vllm/vllm-openai-rocm",
}


def runtime_image(fallback: str | None = None, *, accelerator: str | None = None) -> str:
    """The active hardware profile's model-server image (repository and version).

    The profile resolved for the deployment's accelerator supplies the full image;
    ``fallback`` (then a neutral upstream default) is used only when hardware
    discovery has not resolved a profile yet.
    """
    profile = active_profile(accelerator)
    if profile and profile.deployment.runtime_image:
        return profile.deployment.runtime_image
    return fallback or DEFAULT_RUNTIME_IMAGE


def _profile_images_by_repository() -> dict[str, str]:
    """Map each profile's runtime-image repository to that profile's full image."""
    images: dict[str, str] = {}
    for profile in all_profiles():
        pinned = profile.deployment.runtime_image
        if pinned:
            images[_image_repository(pinned)] = pinned
    return images


def pin_runtime_image(image: str, *, accelerator: str | None = None) -> str:
    """Normalize a managed model-server image to the profile that owns it.

    The hardware profiles own both repository and version. An image whose
    repository a profile defines is replaced with that profile's full image (so a
    GPU config keeps the GPU pin even when the active process accelerator is
    unset); a managed family with no owning profile falls back to the active
    profile. Custom images pass through unchanged.
    """
    repository = _image_repository(image)
    owner = _profile_images_by_repository().get(repository)
    if owner:
        return owner
    profile = active_profile(accelerator)
    pinned = profile.deployment.runtime_image if profile else None
    if pinned and repository in _MANAGED_MODEL_IMAGE_REPOSITORIES:
        return pinned
    return image


def set_accelerator_request(container: dict, claim: dict | None, count: int, *, accelerator: str | None = None) -> None:
    """Set a tensor-parallel accelerator count on a rendered pod.

    DRA profiles rewrite the ResourceClaimTemplate count; extended-resource
    profiles set the container's ``resources.limits``/``requests`` entry for the
    profile's resource name.
    """
    if claim is not None:
        request = claim["spec"]["spec"]["devices"]["requests"][0]
        request.setdefault("exactly", {})["count"] = count
        return
    name = resource_name(accelerator=accelerator)
    if not name:
        raise ValueError("hardware profile defines neither a DRA claim nor an extended resource name")
    resources = container.setdefault("resources", {})
    resources.setdefault("limits", {})[name] = str(count)
    resources.setdefault("requests", {})[name] = str(count)
