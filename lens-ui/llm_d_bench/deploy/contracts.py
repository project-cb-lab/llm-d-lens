"""Versioned input and output contracts for the deployment module.

The module owns deployment lifecycle contracts only. Configuration owns
validation and generation of immutable configuration artifacts; deploy only
consumes those artifacts through a selected deployment provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class DeploymentStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    RENDERED = "rendered"
    DEPLOYING = "deploying"
    READY = "ready"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CLEANED = "cleaned"
    CLEANED_UP = "cleaned_up"


class DeploymentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    CLEANED = "cleaned"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class DeploymentCaseStatus(StrEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    DEPLOYING = "deploying"
    READY = "ready"
    FAILED = "failed"
    STOPPED = "stopped"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"
    CLEANED_UP = "cleaned_up"


class VersionedPayload(BaseModel):
    """Opaque domain payload with an explicitly declared producer schema."""

    schema_version: str = Field(min_length=1)
    value: dict[str, Any] = Field(default_factory=dict)


class ProblemDetails(BaseModel):
    status: int
    title: str
    detail: str | None = None
    code: str | None = None


class DeploymentMetadata(BaseModel):
    """User-owned descriptive labels for a deployment execution.

    Deployment lifecycle writers never produce these values; they are edited
    only through the deployment management API. Keeping them out of
    ``provenance`` preserves provenance as immutable origin evidence.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1000)


class DeploymentMetadataUpdateRequest(BaseModel):
    """Allowlisted metadata patch; every other deployment field is immutable."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> DeploymentMetadataUpdateRequest:
        if self.display_name is None and self.description is None:
            raise ValueError("at least one of display_name or description is required")
        return self

    def apply(self, current: DeploymentMetadata) -> DeploymentMetadata:
        """Merge only the explicitly supplied fields onto the stored metadata."""
        return DeploymentMetadata(
            display_name=(current.display_name if self.display_name is None else self.display_name.strip()),
            description=(current.description if self.description is None else self.description.strip()),
        )


class DeployableConfiguration(BaseModel):
    """Immutable Configuration output accepted by the Deploy module."""

    schema_version: str = Field(default="deployable-configuration.v1", min_length=1)
    type: str = Field(min_length=1)
    format: str = Field(min_length=1)
    content: dict[str, Any] = Field(default_factory=dict)
    provider_ref: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class RuntimeBinding(BaseModel):
    """Environment-specific values bound by Deploy without changing Configuration ownership."""

    schema_version: str = Field(default="runtime-binding.v1", min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    cluster_session_id: str | None = None


class DeploymentRunCreateRequest(BaseModel):
    configurations: list[DeployableConfiguration] = Field(min_length=1)
    failure_policy: str = Field(default="continue", pattern="^(stop|continue)$")
    provenance: dict[str, Any] = Field(default_factory=dict)
    runtime_binding: RuntimeBinding | None = None


class ConfigurationArtifact(BaseModel):
    """Immutable configuration file emitted by the configuration module."""

    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    schema_version: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    provider_ref: str = Field(min_length=1)
    media_type: str = Field(default="application/yaml", min_length=1)
    checksum: str = Field(min_length=1)
    content_ref: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def artifact_requires_content_or_reference(self) -> ConfigurationArtifact:
        if self.content_ref is None and self.content is None:
            raise ValueError("configuration artifact requires content or content_ref")
        return self


class DeploymentCreateRequest(BaseModel):
    """Deploy input supplied by configuration and consumed by a provider."""

    request_id: str = Field(default_factory=lambda: str(uuid4()))
    input_schema_version: str = Field(default="v1", min_length=1)
    configuration_artifacts: list[ConfigurationArtifact] = Field(min_length=1)
    deployment_policy: VersionedPayload = Field(default_factory=lambda: VersionedPayload(schema_version="v1"))
    cluster_snapshot_ref: str | None = None
    cluster_snapshot: VersionedPayload | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class DeploymentCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    ordinal: int = Field(ge=0)
    source_configuration_ordinal: int = Field(ge=0)
    provider_ref: str = Field(min_length=1)
    component: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    create_request: DeploymentCreateRequest
    status: DeploymentCaseStatus = DeploymentCaseStatus.QUEUED
    attempt: int = Field(default=1, ge=1)
    parent_case_id: str | None = None
    execution_id: str | None = None
    failure: ProblemDetails | None = None


class DeploymentRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: DeploymentRunStatus = DeploymentRunStatus.QUEUED
    source_configurations: list[DeployableConfiguration] = Field(min_length=1)
    cases: list[DeploymentCase] = Field(default_factory=list)
    failure_policy: str = Field(default="continue", pattern="^(stop|continue)$")
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DeploymentKillRequest(BaseModel):
    """Explicit request to terminate a deployment created by deploy."""

    request_id: str = Field(default_factory=lambda: str(uuid4()))
    input_schema_version: str = Field(default="v1", min_length=1)
    execution_id: str = Field(min_length=1)
    expected_namespace: str | None = None
    reason: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class DeploymentLogRequest(BaseModel):
    """Cursor-based query for logs belonging to a deployment execution."""

    execution_id: str = Field(min_length=1)
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=1_000)
    follow: bool = False
    sources: list[str] = Field(default_factory=list)


class DeploymentLogEntry(BaseModel):
    """One normalized deployment log line without provider credentials."""

    timestamp: datetime
    source: str = Field(min_length=1)
    message: str
    severity: str | None = None


class DeploymentLogPage(BaseModel):
    """A page of deployment logs and any durable evidence created while reading."""

    execution_id: str = Field(min_length=1)
    entries: list[DeploymentLogEntry] = Field(default_factory=list)
    next_cursor: str | None = None
    complete: bool = True
    evidence_refs: list[str] = Field(default_factory=list)


class DeploymentArtifact(BaseModel):
    """Immutable rendered deployment evidence produced before cluster mutation."""

    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    artifact_hash: str = Field(min_length=1)
    configuration_artifact_ids: list[str] = Field(min_length=1)
    source_ref: str | None = None
    manifest_ref: str | None = None
    manifest_checksum: str | None = None
    values_checksum: str | None = None
    rendered_payload: VersionedPayload
    created_at: datetime = Field(default_factory=utcnow)


class DeploymentEndpoint(BaseModel):
    """Ready endpoint passed to benchmark validation without exposing credentials."""

    url: str = Field(min_length=1)
    protocol: str = Field(default="http", min_length=1)
    service_ref: str | None = None
    model_ref: str | None = None
    baseline_url: str | None = Field(
        default=None,
        description=(
            "Optional second endpoint on the same deployment execution that bypasses the "
            "Guide's routing layer entirely (e.g. a plain Kubernetes Service round-robining "
            "across the same pods). Lets Evaluate compare routed-vs-unrouted traffic on "
            "identical, already-warmed pods without provisioning a second deployment."
        ),
    )


class BenchmarkDeploymentInput(BaseModel):
    """Stable deployment output projection consumed by the benchmark module."""

    execution_id: str
    artifact: DeploymentArtifact
    endpoint: DeploymentEndpoint
    namespace: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class SimulationDeploymentInput(BaseModel):
    """Stable deployment output projection consumed by the future simulation module."""

    execution_id: str
    artifact: DeploymentArtifact
    deployment_status: DeploymentStatus
    endpoint: DeploymentEndpoint | None = None
    namespace: str | None = None
    resource_snapshot: VersionedPayload | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class DeploymentExecution(BaseModel):
    """Deployment lifecycle output retained for benchmark, simulation, and audit."""

    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    request_id: str
    status: DeploymentStatus
    artifact: DeploymentArtifact
    endpoint: DeploymentEndpoint | None = None
    forwarded_endpoint: str | None = None
    # Setup audit only; current monitor state is owned by the Monitoring API.
    monitoring_setup: dict[str, Any] | None = None
    namespace: str | None = None
    configuration_artifacts: list[ConfigurationArtifact] = Field(min_length=1)
    resource_snapshot: VersionedPayload | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    # The concrete data plane the framework resolved at deploy time (see
    # llm_d_bench/deploy/data_plane.py): "shared_gateway", "standalone_router",
    # "direct" or "external". A top-level field (not nested in `diagnostics`)
    # so periodic readiness refreshes, which only overwrite `diagnostics`,
    # never clobber it.
    data_plane: str | None = None
    metadata: DeploymentMetadata = Field(default_factory=DeploymentMetadata)
    evidence_refs: list[str] = Field(default_factory=list)
    diagnostics: VersionedPayload | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def ready_execution_requires_endpoint(self) -> DeploymentExecution:
        if self.status == DeploymentStatus.READY and self.endpoint is None:
            raise ValueError("ready deployment execution requires an endpoint")
        return self

    def benchmark_input(self) -> BenchmarkDeploymentInput:
        """Project a ready execution into the benchmark module's dependency API."""
        if self.status != DeploymentStatus.READY or self.endpoint is None:
            raise ValueError("only a ready deployment execution can be benchmarked")
        return BenchmarkDeploymentInput(
            execution_id=self.execution_id,
            artifact=self.artifact,
            endpoint=self.endpoint,
            namespace=self.namespace,
            provenance=self.provenance,
        )

    def simulation_input(self) -> SimulationDeploymentInput:
        """Project execution evidence for future simulation and TCO calculation."""
        return SimulationDeploymentInput(
            execution_id=self.execution_id,
            artifact=self.artifact,
            deployment_status=self.status,
            endpoint=self.endpoint,
            namespace=self.namespace,
            resource_snapshot=self.resource_snapshot,
            provenance=self.provenance,
        )
