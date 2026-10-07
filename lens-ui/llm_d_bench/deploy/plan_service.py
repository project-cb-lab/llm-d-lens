"""Creation and retry operations for Configuration-derived deployment runs."""

from __future__ import annotations

import yaml

from llm_d_bench.configuration.managed_environment import (
    admin_managed_environment_error,
    admin_managed_environment_names,
    is_admin_managed_environment_variable,
)
from llm_d_bench.deploy.configuration_adapter import DeploymentConfigurationPlanner
from llm_d_bench.deploy.contracts import (
    DeploymentCase,
    DeploymentCaseStatus,
    DeploymentRun,
    DeploymentRunCreateRequest,
    DeploymentRunStatus,
)
from llm_d_bench.deploy.run_store import JsonDeploymentRunStore
from llm_d_bench.utils.artifacts import configuration_checksum, text_checksum, validate_configuration_manifest_content


class DeploymentPlanService:
    def __init__(self, store: JsonDeploymentRunStore, planner: DeploymentConfigurationPlanner | None = None) -> None:
        self._store = store
        self._planner = planner or DeploymentConfigurationPlanner()

    def create_run(self, request: DeploymentRunCreateRequest) -> DeploymentRun:
        for configuration in request.configurations:
            self._reject_admin_managed_environment(configuration.content)
        if request.runtime_binding is not None:
            for name in request.runtime_binding.environment:
                if is_admin_managed_environment_variable(name):
                    raise ValueError(admin_managed_environment_error(name))
        configurations = [
            self._apply_runtime_binding(configuration, request) for configuration in request.configurations
        ]
        run = DeploymentRun(
            source_configurations=configurations,
            failure_policy=request.failure_policy,
            provenance={
                **request.provenance,
                **(
                    {"runtime_binding": request.runtime_binding.model_dump(mode="json")}
                    if request.runtime_binding
                    else {}
                ),
            },
        )
        run.cases = self._planner.plan(configurations, run_id=run.id, provenance=run.provenance)
        return self._store.create_run(run)

    @staticmethod
    def _reject_admin_managed_environment(content: object) -> None:
        if not isinstance(content, dict):
            return
        custom = content.get("customParameters") or content.get("custom_parameters") or []
        for name in admin_managed_environment_names(custom):
            raise ValueError(admin_managed_environment_error(name))

    @staticmethod
    def _apply_runtime_binding(configuration, request: DeploymentRunCreateRequest):
        binding = request.runtime_binding
        if binding is None or not binding.environment:
            return configuration
        runtime = configuration.content.get("runtime")
        if not isinstance(runtime, dict):
            raise ValueError("deployable configuration requires content.runtime before runtime binding")
        if configuration.checksum != configuration_checksum(configuration.content):
            raise ValueError("deployable configuration checksum does not match content")
        content = {
            **configuration.content,
            "runtime": {
                **runtime,
                "environment": {
                    **(runtime.get("environment") if isinstance(runtime.get("environment"), dict) else {}),
                    **binding.environment,
                },
            },
        }
        provenance = {
            **configuration.provenance,
            "configuration_checksum": configuration.checksum,
            "runtime_binding_schema": binding.schema_version,
        }
        if isinstance(content.get("officialGuide"), dict):
            # Validate the immutable source before deriving the deploy-time copy.
            manifest, source_checksum, _, _ = validate_configuration_manifest_content(
                configuration.content, configuration.provider_ref
            )
            documents = list(yaml.safe_load_all(manifest))
            for document in documents:
                if not isinstance(document, dict) or document.get("kind") != "Deployment":
                    continue
                containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                for container in containers:
                    if container.get("name") != "modelserver":
                        continue
                    container["env"] = [
                        entry for entry in container.get("env", []) if entry["name"] not in binding.environment
                    ] + [{"name": name, "value": value} for name, value in binding.environment.items()]
            bound_manifest = yaml.safe_dump_all(documents, sort_keys=False)
            content["officialGuide"] = {
                **content["officialGuide"],
                "renderedManifest": bound_manifest,
                "manifestChecksum": text_checksum(bound_manifest),
            }
            validate_configuration_manifest_content(content, configuration.provider_ref)
            provenance.update(
                source_manifest_checksum=source_checksum,
                manifest_checksum=text_checksum(bound_manifest),
            )
        return configuration.model_copy(
            update={
                "content": content,
                "checksum": configuration_checksum(content),
                "provenance": provenance,
            }
        )

    def retry_case(self, run_id: str, case_id: str) -> DeploymentCase:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"deployment run not found: {run_id}")
        case = next((item for item in run.cases if item.id == case_id), None)
        if case is None:
            raise ValueError(f"deployment case not found: {case_id}")
        root_case_id = case.parent_case_id or case.id
        attempts = [item for item in run.cases if (item.parent_case_id or item.id) == root_case_id]
        latest = max(attempts, key=lambda item: item.attempt)
        if latest.id != case.id or case.status not in {DeploymentCaseStatus.FAILED, DeploymentCaseStatus.STOPPED}:
            raise ValueError("only the latest stopped or failed deployment case may be retried")
        retry = case.model_copy(
            update={
                "id": None,
                "status": DeploymentCaseStatus.QUEUED,
                "attempt": case.attempt + 1,
                "parent_case_id": root_case_id,
                "execution_id": None,
                "failure": None,
            }
        )
        retry = DeploymentCase.model_validate(retry.model_dump(exclude_none=True))
        run.cases.append(retry)
        run.status = DeploymentRunStatus.QUEUED
        run.finished_at = None
        self._store.save_run(run)
        return retry
