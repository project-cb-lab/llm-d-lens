# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""API models for the configuration resolution pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_d_bench.deploy.contracts import DeployableConfiguration

CandidateSourceName = Literal[
    "aic",
    "baseline_search",
    "pd_search",
    "epd_search",
    "tiered_cache_search",
    "historical",
    "manual",
]
ConfigurationType = Literal["baseline", "pd", "epd", "tiered_cache"]


class CandidateSource(BaseModel):
    name: CandidateSourceName
    run_id: str | None = None
    result_id: str | None = None


class SourceConfiguration(BaseModel):
    configuration_id: str
    result_id: str | None = None
    input_mode: Literal["source_result", "form", "yaml"] = "source_result"
    payload: dict[str, Any] | str


class ResolveTarget(BaseModel):
    cluster_id: str | None = None


class ResolveRequest(BaseModel):
    candidate_source: CandidateSource
    configurations: list[SourceConfiguration] = Field(min_length=1)
    target: ResolveTarget = Field(default_factory=ResolveTarget)


class ValidationResult(BaseModel):
    status: Literal["valid", "valid_with_warnings", "invalid"]
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CandidateConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    type: ConfigurationType
    candidate_source: CandidateSource
    target: dict[str, Any]
    serving: dict[str, Any] | None = None
    encode: dict[str, Any] | None = None
    prefill: dict[str, Any] | None = None
    decode: dict[str, Any] | None = None
    cache: dict[str, Any] | None = None
    resources: dict[str, Any] | None = None
    network: dict[str, Any] | None = None
    performance: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None


class ResolveResult(BaseModel):
    configuration_id: str
    candidate_config: CandidateConfig
    validation: ValidationResult


class ResolveResponse(BaseModel):
    results: list[ResolveResult]


class ClusterReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    session_id: str = Field(min_length=36, max_length=36)
    connection: Literal["managed-kubeconfig"]


class GuideSource(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repository: str = Field(min_length=1)
    requested_ref: str = Field(min_length=1, alias="requestedRef")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    guide: str = Field(min_length=1)
    accelerator: str = Field(min_length=1)
    model_server: str = Field(min_length=1, alias="modelServer")
    variant: str = Field(min_length=1)
    files: list[str] = Field(min_length=1)


class DeploymentEndpointContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol: Literal["http", "https"]
    service_name: str = Field(min_length=1, alias="serviceName")
    port: int = Field(gt=0, le=65535)


class DeploymentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    readiness_deployments: list[str] = Field(min_length=1, alias="readinessDeployments")
    endpoint: DeploymentEndpointContract


class ModelSecretConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["none", "host", "existing-secret"] = "none"
    source_namespace: str | None = Field(default=None, alias="sourceNamespace")
    source_name: str | None = Field(default=None, alias="sourceName")

    @model_validator(mode="after")
    def existing_secret_has_source(self) -> ModelSecretConfiguration:
        if self.mode == "existing-secret" and (not self.source_namespace or not self.source_name):
            raise ValueError("existing-secret mode requires sourceNamespace and sourceName")
        return self


class RenderOptions(BaseModel):
    format: Literal["manifest"]
    guide_ref: str = Field(min_length=1)
    template_ref: str = Field(min_length=1)
    guide_source: GuideSource
    rendered_manifest: str = Field(min_length=1)
    deployment_bundle: dict[str, Any] | None = None
    reference_manifest: str | None = Field(default=None, min_length=1)
    cluster_ref: ClusterReference
    deployment: DeploymentContract
    model_secret: ModelSecretConfiguration = Field(default_factory=ModelSecretConfiguration, alias="modelSecret")
    deployment_name: str | None = Field(default=None, min_length=1, max_length=120, alias="deploymentName")

    @model_validator(mode="after")
    def source_matches_provider(self) -> RenderOptions:
        if self.guide_source.guide != self.guide_ref:
            raise ValueError("guide_source.guide must match guide_ref")
        return self


class RenderRequest(BaseModel):
    candidate_config: CandidateConfig
    render: RenderOptions


class RenderResponse(BaseModel):
    deployable_configuration: DeployableConfiguration
    metadata: dict[str, Any]


class FileOptions(BaseModel):
    name: str | None = None


class SaveRequest(BaseModel):
    deployable_configuration: DeployableConfiguration
    file: FileOptions = Field(default_factory=FileOptions)


class ConfigurationFile(BaseModel):
    uri: str | None = None
    type: ConfigurationType
    file_name: str
    format: str
    path: str
    size: int
    sha256: str


class ConfigurationArtifactRecord(BaseModel):
    artifact_id: str
    schema_version: str = "configuration-artifact.v1"
    revision: int = 1
    status: Literal["published"] = "published"
    deployable_configuration: DeployableConfiguration
    configuration_file: ConfigurationFile
    created_at: datetime


class SaveResponse(BaseModel):
    configuration_file: ConfigurationFile
    artifact: ConfigurationArtifactRecord
