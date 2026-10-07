"""Accelerator provider registry.

Each supported accelerator is a small provider that mirrors the ``cluster_stack``
service contract. Adding a new hardware type means registering a new provider, not
copying routes.
"""

from __future__ import annotations

from typing import Protocol

from llm_d_bench.monitoring.cluster_stack.models import ClusterStackStatusResponse

from .errors import AcceleratorError
from .models import (
    AcceleratorCapabilitiesResponse,
    AcceleratorCapability,
    AcceleratorInstallRequest,
    AcceleratorPreflightResponse,
    AcceleratorStatusResponse,
    GpuAccess,
)


class AcceleratorProvider(Protocol):
    def capability(self) -> AcceleratorCapability: ...

    async def discover_status(
        self,
        namespace: str,
        cluster_id: str | None,
        *,
        access_mode: GpuAccess | None = None,
        monitoring_namespace: str | None = None,
        active_operation_id: str | None = None,
    ) -> AcceleratorStatusResponse: ...

    async def preflight(
        self,
        request: AcceleratorInstallRequest,
        cluster_id: str | None,
        *,
        monitoring_status: ClusterStackStatusResponse | None = None,
    ) -> AcceleratorPreflightResponse: ...

    async def prepare_nodes(
        self,
        cluster_id: str | None,
        *,
        access_mode: GpuAccess | None,
    ) -> None: ...

    def install_argv(
        self,
        request: AcceleratorInstallRequest,
        *,
        monitoring_namespace: str,
        monitoring_release: str,
    ) -> list[str]: ...


_ACCELERATOR_REGISTRY: dict[str, AcceleratorProvider] = {}


def register(provider: AcceleratorProvider) -> None:
    _ACCELERATOR_REGISTRY[provider.capability().type] = provider


def get_provider(accelerator: str) -> AcceleratorProvider:
    provider = _ACCELERATOR_REGISTRY.get(accelerator)
    if provider is None:
        raise AcceleratorError(
            "ACCELERATOR_NOT_SUPPORTED",
            f"Accelerator {accelerator!r} is not supported",
            status_code=404,
        )
    return provider


def capabilities() -> AcceleratorCapabilitiesResponse:
    return AcceleratorCapabilitiesResponse(
        accelerators=[provider.capability() for provider in _ACCELERATOR_REGISTRY.values()]
    )
