"""DTOs for the cluster monitoring stack API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ClusterStackStatus = Literal[
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
MonitoringMode = Literal["central", "individual"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClusterStackInstallRequest(StrictModel):
    namespace: str = "llm-d-monitoring"
    mode: MonitoringMode = "central"
    enable_tls: bool = False
    reinstall: bool = False

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        from .service import validate_namespace

        return validate_namespace(value)


class ClusterSummary(StrictModel):
    context: str | None = None
    reachable: bool
    platform: Literal["kubernetes", "openshift", "unknown"] = "unknown"


class HelmReleaseSummary(StrictModel):
    name: str = "llmd"
    status: str | None = None
    chart: str | None = None
    revision: int | None = None


class ComponentDiagnostic(StrictModel):
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    resource: str | None = None


class ClusterStackComponent(StrictModel):
    name: str
    status: ComponentStatus
    kind: Literal["workload", "configmap", "crd"] = "workload"
    ready: int | None = None
    desired: int | None = None
    source: Literal["llmd", "cluster", "none"] = "none"
    diagnostics: list[ComponentDiagnostic] = Field(default_factory=list)


class ClusterStackStatusResponse(StrictModel):
    cluster: ClusterSummary
    namespace: str
    release: HelmReleaseSummary
    status: ClusterStackStatus
    message: str
    components: list[ClusterStackComponent] = Field(default_factory=list)
    active_operation_id: str | None = None
    observed_at: datetime
    stale: bool = False


class ObservabilityLink(StrictModel):
    kind: Literal["prometheus", "grafana"]
    label: str
    available: bool
    url: str | None = None
    service: str | None = None
    namespace: str | None = None
    port: int | None = None
    local_port: int | None = None
    dashboard_path: str | None = None
    message: str | None = None


class ClusterStackLinksResponse(StrictModel):
    namespace: str
    links: list[ObservabilityLink] = Field(default_factory=list)


class PreflightCheck(StrictModel):
    name: str
    passed: bool
    message: str
    blocking: bool = True


class ClusterStackPreflightResponse(StrictModel):
    allowed: bool
    status: ClusterStackStatus
    checks: list[PreflightCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    command_preview: list[str] = Field(default_factory=list)


class OperationLogEntry(StrictModel):
    sequence: int
    timestamp: datetime
    level: Literal["info", "warning", "error"] = "info"
    message: str


class OperationError(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ClusterStackOperationResponse(StrictModel):
    operation_id: str
    kind: Literal["install"] = "install"
    status: OperationStatus
    phase: str
    namespace: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    logs: list[OperationLogEntry] = Field(default_factory=list)
    error: OperationError | None = None
