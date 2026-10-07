"""Configuration-output parsing and deployment-case planning."""

from __future__ import annotations

import json
from typing import Protocol
from uuid import uuid4

from llm_d_bench.deploy.contracts import (
    ConfigurationArtifact,
    DeployableConfiguration,
    DeploymentCase,
    DeploymentCreateRequest,
    VersionedPayload,
)
from llm_d_bench.utils.artifacts import configuration_checksum, deployment_manifest


class DeployableConfigurationAdapter(Protocol):
    def supports(self, configuration: DeployableConfiguration) -> bool: ...

    def plan(
        self,
        configuration: DeployableConfiguration,
        *,
        run_id: str,
        source_configuration_ordinal: int,
        first_ordinal: int,
        run_provenance: dict[str, object],
    ) -> list[DeploymentCase]: ...


class GuideHelmConfigurationAdapter:
    """Pass one complete published configuration to its registered Guide."""

    def supports(self, configuration: DeployableConfiguration) -> bool:
        return configuration.format in {"helm", "manifest"}

    def plan(
        self,
        configuration: DeployableConfiguration,
        *,
        run_id: str,
        source_configuration_ordinal: int,
        first_ordinal: int,
        run_provenance: dict[str, object],
    ) -> list[DeploymentCase]:
        self._validate(configuration)
        provider_ref = configuration.provider_ref
        artifact = ConfigurationArtifact(
            schema_version=configuration.schema_version,
            kind="configuration-manifest" if configuration.format == "manifest" else "guide-helm-values",
            provider_ref=provider_ref,
            media_type="application/json",
            checksum=configuration.checksum,
            content=json.dumps(configuration.content, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )
        return [
            DeploymentCase(
                id=str(uuid4()),
                run_id=run_id,
                ordinal=first_ordinal,
                source_configuration_ordinal=source_configuration_ordinal,
                provider_ref=provider_ref,
                component=provider_ref,
                create_request=DeploymentCreateRequest(
                    input_schema_version="deployment-create-request.v1",
                    configuration_artifacts=[artifact],
                    deployment_policy=VersionedPayload(
                        schema_version="deployment-policy.v1",
                        value={
                            "guide_name": provider_ref,
                            "format": configuration.format,
                            "deployment_name": configuration.provenance.get("deployment_name")
                            or configuration.content["model"]["name"],
                            "namespace_policy": configuration.provenance.get("namespace_policy"),
                            "model_secret": (
                                (configuration.content.get("officialGuide") or {}).get("modelSecret")
                                or configuration.content.get("modelSecret")
                            ),
                        },
                    ),
                    provenance={
                        **run_provenance,
                        **configuration.provenance,
                        "deployment_run_id": run_id,
                        "source_configuration_ordinal": source_configuration_ordinal,
                        "guide_id": provider_ref,
                    },
                ),
            )
        ]

    @staticmethod
    def _validate(configuration: DeployableConfiguration) -> None:
        if configuration.format == "manifest":
            deployment_manifest(configuration)
        elif configuration.checksum != configuration_checksum(configuration.content):
            raise ValueError("deployable configuration checksum does not match content")
        model = configuration.content.get("model")
        if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not model["name"]:
            raise ValueError("helm configuration requires content.model.name")


class DeploymentConfigurationPlanner:
    """Select registered adapters and produce ordered deploy-owned cases."""

    def __init__(self, adapters: list[DeployableConfigurationAdapter] | None = None) -> None:
        self._adapters = adapters or [GuideHelmConfigurationAdapter()]

    def plan(
        self,
        configurations: list[DeployableConfiguration],
        *,
        run_id: str,
        provenance: dict[str, object] | None = None,
    ) -> list[DeploymentCase]:
        cases: list[DeploymentCase] = []
        for source_ordinal, configuration in enumerate(configurations):
            adapter = next((item for item in self._adapters if item.supports(configuration)), None)
            if adapter is None:
                raise ValueError(
                    f"unsupported deployable configuration: type={configuration.type}, format={configuration.format}"
                )
            cases.extend(
                adapter.plan(
                    configuration,
                    run_id=run_id,
                    source_configuration_ordinal=source_ordinal,
                    first_ordinal=len(cases),
                    run_provenance=provenance or {},
                )
            )
        return cases
