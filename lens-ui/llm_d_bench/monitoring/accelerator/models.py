"""DTOs for the accelerator observability API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from llm_d_bench.monitoring.cluster_stack.models import (
    ComponentDiagnostic,
    HelmReleaseSummary,
    ObservabilityLink,
    OperationError,
    OperationLogEntry,
    PreflightCheck,
    StrictModel,
)

# Open hardware identity. The concrete value is validated against a registered
# hardware profile (``llm_d_bench.hardware``) instead of a closed literal, so a
# new accelerator provider does not require editing this type.
AcceleratorType = str
GpuAccess = Literal["dra", "plugin"]
AcceleratorStatus = Literal[
    "unknown",
    "absent",
    "installing",
    "ready",
    "degraded",
    "unsupported",
    "unreachable",
]
ComponentStatus = Literal["ready", "progressing", "degraded", "missing", "external", "unknown"]
OperationStatus = Literal["queued", "running", "succeeded", "failed"]

INTEL_GPU_RELEASE_NAME = "xpumd"
INTEL_GPU_DEFAULT_NAMESPACE = "intel-xpumd"


class AcceleratorCapability(StrictModel):
    type: AcceleratorType
    display_name: str
    access_modes: list[GpuAccess]
    release_name: str
    default_namespace: str


class AcceleratorCapabilitiesResponse(StrictModel):
    accelerators: list[AcceleratorCapability]


class AcceleratorInstallRequest(StrictModel):
    accelerator: AcceleratorType = "intel_gpu"
    access_mode: GpuAccess = "dra"
    namespace: str = INTEL_GPU_DEFAULT_NAMESPACE

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        from .service import validate_namespace

        return validate_namespace(value)

    @field_validator("accelerator")
    @classmethod
    def validate_accelerator(cls, value: str) -> str:
        """Reject accelerators no registered hardware profile supports."""
        from llm_d_bench.hardware.resolver import resolve_by_accelerator_key

        if resolve_by_accelerator_key(value) is None:
            raise ValueError(f"accelerator {value!r} is not a registered hardware profile")
        return value


class AcceleratorComponent(StrictModel):
    name: str
    status: ComponentStatus
    kind: Literal["workload", "configmap", "crd", "node"] = "workload"
    ready: int | None = None
    desired: int | None = None
    # Open source label so a new hardware provider (e.g. "dcgm_exporter") needs
    # no DTO edit; Intel keeps emitting "xpumd"/"cluster"/"none".
    source: str = "none"
    diagnostics: list[ComponentDiagnostic] = Field(default_factory=list)


class AccessModeAvailability(StrictModel):
    mode: GpuAccess
    available: bool
    detected: bool
    message: str | None = None


class AcceleratorLinksResponse(StrictModel):
    accelerator: AcceleratorType
    links: list[ObservabilityLink] = Field(default_factory=list)


class AcceleratorStatusResponse(StrictModel):
    accelerator: AcceleratorType
    access_mode: GpuAccess | None = None
    access_modes: list[AccessModeAvailability] = Field(default_factory=list)
    cluster_reachable: bool
    context: str | None = None
    namespace: str
    release: HelmReleaseSummary
    status: AcceleratorStatus
    message: str
    components: list[AcceleratorComponent] = Field(default_factory=list)
    active_operation_id: str | None = None
    observed_at: datetime
    stale: bool = False


class AcceleratorPreflightResponse(StrictModel):
    allowed: bool
    status: AcceleratorStatus
    checks: list[PreflightCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    command_preview: list[str] = Field(default_factory=list)


class AcceleratorOperationResponse(StrictModel):
    operation_id: str
    kind: Literal["install"] = "install"
    status: OperationStatus
    phase: str
    accelerator: AcceleratorType
    access_mode: GpuAccess | None = None
    namespace: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    logs: list[OperationLogEntry] = Field(default_factory=list)
    error: OperationError | None = None
