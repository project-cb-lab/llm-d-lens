"""Decorator registry with automatic backend module discovery."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, TypeVar

from ..errors import SimulationConfigurationError
from ..models import BackendDescriptor, ScenarioDescriptor

if TYPE_CHECKING:
    from .base import Backend

BackendType = TypeVar("BackendType")
_BACKENDS: dict[str, Backend] = {}
_DISCOVERED = False
_INFRASTRUCTURE_MODULES = {"analytics", "base", "registry"}


def register_backend(backend_type: BackendType) -> BackendType:
    from .base import Backend

    if not isinstance(backend_type, type) or not issubclass(backend_type, Backend):
        raise TypeError("Registered Simulation backends must inherit Backend")
    backend = backend_type()
    if not backend.name:
        raise ValueError("Simulation backend name must not be empty")
    if backend.name in _BACKENDS:
        raise ValueError(f"Simulation backend '{backend.name}' is already registered")
    _BACKENDS[backend.name] = backend
    return backend_type


def discover_backends() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    package = importlib.import_module(__package__)
    for module in pkgutil.iter_modules(package.__path__):
        if module.name.startswith("_") or module.name in _INFRASTRUCTURE_MODULES:
            continue
        importlib.import_module(f"{__package__}.{module.name}")
    _DISCOVERED = True


def list_backends() -> list[BackendDescriptor]:
    discover_backends()
    return [_BACKENDS[name].descriptor() for name in sorted(_BACKENDS)]


def list_scenarios() -> list[ScenarioDescriptor]:
    scenarios: dict[str, ScenarioDescriptor] = {}
    for backend in list_backends():
        for scenario in backend.scenarios:
            current = scenarios.get(scenario.name)
            supported = list(
                dict.fromkeys(
                    [
                        *(current.supported_backends if current is not None else []),
                        backend.name,
                    ]
                )
            )
            scenarios[scenario.name] = scenario.model_copy(update={"supported_backends": supported})
    return list(scenarios.values())


def _get_registered_backend(name: str) -> Backend:
    discover_backends()
    backend = _BACKENDS.get(name)
    if backend is None:
        raise SimulationConfigurationError(f"Unknown simulation backend '{name}'")
    return backend


def get_backend(name: str) -> Backend:
    backend = _get_registered_backend(name)
    descriptor = backend.descriptor()
    if not descriptor.available:
        raise SimulationConfigurationError(
            f"Simulation backend '{name}' is unavailable: {descriptor.unavailable_reason}"
        )
    return backend
