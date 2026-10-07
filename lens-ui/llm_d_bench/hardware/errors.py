"""Domain errors for the hardware provider plugin mechanism."""

from __future__ import annotations

from llm_d_bench.core.exceptions import DomainError


class HardwareError(DomainError):
    """Base error for hardware profile loading and resolution."""

    code = "hardware_error"
    status_code = 400


class HardwareRegistrationError(HardwareError):
    """A profile or provider conflicts with an already-registered one."""

    code = "hardware_registration_error"
    status_code = 409


class HardwareProfileInvalidError(HardwareError):
    """A profile payload failed JSON Schema validation."""

    code = "hardware_profile_invalid"
    status_code = 422


class HardwareProfileNotFoundError(HardwareError):
    """No registered profile resolves the requested accelerator identity.

    The code matches the design's ``ACCELERATOR_NOT_REGISTERED`` contract for
    explicit use of an unregistered hardware.
    """

    code = "accelerator_not_registered"
    status_code = 404
