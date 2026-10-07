"""Guide deployment contracts owned by the Deploy module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class GuideDefinition:
    guide_id: str
    source_ref: str
    content_hash: str
    maturity: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    prerequisites: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GuideDeploymentArtifact:
    guide_id: str
    artifact_hash: str
    manifest_ref: str | None = None
    values_checksum: str | None = None
    source_ref: str | None = None
    guide_content_hash: str | None = None
    manifest_checksum: str | None = None
    deployment_contract: dict[str, Any] = field(default_factory=dict)


class GuideAdapter(Protocol):
    """Registered provider lifecycle for one deployment Guide."""

    def discover(self) -> GuideDefinition: ...

    def validate_inputs(
        self,
        definition: GuideDefinition,
        cluster_snapshot: dict[str, Any],
        overrides: dict[str, Any],
    ) -> ValidationResult: ...

    async def render(self, definition: GuideDefinition, overrides: dict[str, Any]) -> GuideDeploymentArtifact: ...

    async def deploy(self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]) -> dict[str, Any]: ...

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult: ...

    async def diagnostics(self, execution: dict[str, Any]) -> dict[str, Any]: ...

    async def stop(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]: ...

    async def rollback(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]: ...

    async def cleanup(
        self, execution: dict[str, Any], artifact: GuideDeploymentArtifact, *, force: bool = False
    ) -> dict[str, Any]: ...
