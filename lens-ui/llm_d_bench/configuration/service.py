# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Configuration resolve, render, and persistence operations."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from llm_d_bench.cluster import require_active_session
from llm_d_bench.deploy.contracts import DeployableConfiguration
from llm_d_bench.utils.artifact_store import artifact_uri, register_artifacts
from llm_d_bench.utils.artifacts import (
    configuration_checksum,
    deployment_manifest,
    text_checksum,
    validate_configuration_cluster_binding,
)
from llm_d_bench.utils.paths import storage_path

from .capability import check_capability
from .errors import ConfigurationPersistenceError
from .guide_settings import validate_configuration_extensions
from .manifest_facts import validate_manifest_facts
from .models import (
    ConfigurationArtifactRecord,
    ConfigurationFile,
    RenderRequest,
    RenderResponse,
    ResolveRequest,
    ResolveResponse,
    ResolveResult,
    SaveRequest,
    SaveResponse,
    ValidationResult,
)
from .normalizers import normalize_configuration
from .validators import validate_candidate

CONFIGURATION_OUTPUT_DIR = storage_path("data", "artifacts", "configurations")
CONFIGURATION_ARTIFACT_DIR = storage_path("data", "metadata", "configurations")


async def resolve_configurations(request: ResolveRequest) -> ResolveResponse:
    results: list[ResolveResult] = []
    for source_configuration in request.configurations:
        try:
            candidate = normalize_configuration(request.candidate_source, source_configuration, request.target)
            supported, reason, capability_warnings = await check_capability(candidate)
            validation = validate_candidate(candidate, capability_warnings)
            if not supported:
                validation = ValidationResult(
                    status="invalid",
                    errors=[reason or "Model and hardware combination is unsupported", *validation.errors],
                    warnings=validation.warnings,
                )
        except ValueError as error:
            # Return one result per selected file so the UI can identify exactly which edit failed.
            fallback_payload: dict[str, Any] = {
                "type": {
                    "baseline_search": "baseline",
                    "pd_search": "pd",
                    "epd_search": "epd",
                    "tiered_cache_search": "tiered_cache",
                }.get(request.candidate_source.name, "baseline"),
                "candidate_source": request.candidate_source,
                "target": request.target.model_dump(exclude_none=True),
            }
            candidate = normalize_configuration(
                request.candidate_source,
                source_configuration.model_copy(update={"payload": fallback_payload}),
                request.target,
            )
            validation = ValidationResult(status="invalid", errors=[str(error)], warnings=[])
        results.append(
            ResolveResult(
                configuration_id=source_configuration.configuration_id,
                candidate_config=candidate,
                validation=validation,
            )
        )
    return ResolveResponse(results=results)


def _component_content(component: dict[str, Any] | None) -> dict[str, Any] | None:
    if not component or (component.get("tensor_parallel_size") is None and component.get("replicas") is None):
        return None
    replicas = component.get("replicas")
    tensor_parallel_size = component.get("tensor_parallel_size")
    if replicas is None or tensor_parallel_size is None:
        raise ValueError("component replicas and tensor_parallel_size must both be explicit")
    content = {"replicaCount": replicas, "tensorParallelSize": tensor_parallel_size}
    if component.get("batch_size") is not None:
        content["batchSize"] = component["batch_size"]
    if component.get("max_model_len") is not None:
        content["maxModelLen"] = component["max_model_len"]
    if component.get("max_num_seqs") is not None:
        content["maxNumSeqs"] = component["max_num_seqs"]
    return content


def _pin_runtime_image(content: dict[str, Any]) -> None:
    """Tag an llm-d model-server runtime image with the pinned stack version.

    The rendered manifest already carries the pinned tag, so the configuration's
    runtime image must match it or the manifest-vs-configuration validation
    fails (see ``configuration.manifest_facts``).
    """
    from llm_d_bench.deploy.providers.hardware_profile import pin_runtime_image  # noqa: PLC0415

    runtime = content.get("runtime")
    if isinstance(runtime, dict) and isinstance(runtime.get("image"), str):
        runtime["image"] = pin_runtime_image(runtime["image"])


def render_configuration(request: RenderRequest) -> RenderResponse:
    candidate = request.candidate_config
    if request.render.template_ref.startswith("upload/"):
        from llm_d_bench.configuration.manifest_edits import validate_manifest_edit

        if not request.render.reference_manifest:
            raise ValueError("Uploaded YAML requires a generated reference from the configuration editor.")
        validate_manifest_edit(request.render.reference_manifest, request.render.rendered_manifest)
    session = require_active_session(request.render.cluster_ref.session_id)
    if session.server_id != request.render.cluster_ref.id:
        raise ValueError("cluster_ref.id does not match the active cluster session")
    serving = candidate.serving or {}
    content: dict[str, Any] = {
        "deploymentType": candidate.type,
        "model": {
            "name": candidate.target.get("model"),
            "maxModelLen": serving.get("max_model_len"),
        },
    }
    component_map = {
        "serving": candidate.serving,
        "encode": candidate.encode,
        "prefill": candidate.prefill,
        "decode": candidate.decode,
    }
    for name, component in component_map.items():
        rendered = _component_content(component)
        if rendered:
            content[name] = rendered
    if candidate.cache:
        content["cache"] = candidate.cache
    if candidate.resources:
        content["resources"] = candidate.resources
    if candidate.network:
        content["network"] = candidate.network
    if candidate.runtime:
        content["runtime"] = candidate.runtime
    custom_parameters = getattr(candidate, "custom_parameters", None)
    if isinstance(custom_parameters, list):
        content["customParameters"] = custom_parameters
    guide_variant = getattr(candidate, "guide_variant", None)
    if isinstance(guide_variant, str) and guide_variant:
        content["guideVariant"] = guide_variant
    guide_settings = getattr(candidate, "guide_settings", None)
    if guide_settings is not None:
        content["guideSettings"] = guide_settings
    rendered_manifest = request.render.rendered_manifest
    content["officialGuide"] = {
        "source": request.render.guide_source.model_dump(mode="json", by_alias=True),
        "renderedManifest": rendered_manifest,
        "manifestChecksum": text_checksum(rendered_manifest),
        "cluster": request.render.cluster_ref.model_dump(mode="json"),
        "deployment": request.render.deployment.model_dump(mode="json", by_alias=True),
        "modelSecret": request.render.model_secret.model_dump(mode="json", by_alias=True),
    }
    content["model"] = {key: value for key, value in content["model"].items() if value is not None}
    if request.render.deployment_bundle is not None:
        content["officialGuide"]["deploymentBundle"] = request.render.deployment_bundle
    _pin_runtime_image(content)
    validate_manifest_facts(content, rendered_manifest)
    validate_configuration_extensions(content, rendered_manifest)

    provenance = {
        "candidate_source": candidate.candidate_source.model_dump(mode="json"),
        "guide_ref": request.render.guide_ref,
        "template_ref": request.render.template_ref,
        "guide_source": request.render.guide_source.model_dump(mode="json", by_alias=True),
        "cluster_ref": request.render.cluster_ref.model_dump(mode="json"),
        "manifest_checksum": content["officialGuide"]["manifestChecksum"],
        "renderer_version": "1.0",
    }
    if request.render.deployment_name:
        provenance["deployment_name"] = request.render.deployment_name
    return RenderResponse(
        deployable_configuration=DeployableConfiguration(
            type=candidate.type,
            format=request.render.format,
            content=content,
            provider_ref=request.render.guide_ref,
            checksum=configuration_checksum(content),
            provenance=provenance,
        ),
        metadata=provenance,
    )


def _safe_file_name(requested: str | None, config_type: str, file_format: str) -> str:
    extension = ".yaml" if file_format in {"helm", "kustomize", "manifest"} else ".json"
    candidate = Path(requested or f"{config_type}-configuration{extension}").name
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate).strip(".-")
    if not candidate:
        candidate = f"{config_type}-configuration{extension}"
    if not Path(candidate).suffix:
        candidate += extension
    return candidate


def save_configuration(request: SaveRequest) -> SaveResponse:
    deployable = request.deployable_configuration
    if deployable.checksum != configuration_checksum(deployable.content):
        raise ValueError("deployable configuration checksum does not match content")
    session = require_active_session(deployable.provenance.get("cluster_ref", {}).get("session_id"))
    validate_configuration_cluster_binding(deployable, session)
    manifest, manifest_checksum, _deployment = deployment_manifest(deployable)
    validate_manifest_facts(deployable.content, manifest)
    validate_configuration_extensions(deployable.content, manifest)
    edited_from = deployable.provenance.get("edited_from_artifact_id")
    if edited_from:
        from llm_d_bench.configuration.manifest_edits import validate_manifest_edit

        original = get_configuration_artifact(edited_from)
        if original is None:
            raise ValueError(
                "The original configuration is unavailable; use the configuration editor to regenerate it."
            )
        validate_manifest_edit(original.deployable_configuration.content["officialGuide"]["renderedManifest"], manifest)
    file_name = _safe_file_name(request.file.name, deployable.type, deployable.format)
    artifact_id = str(uuid4())
    try:
        CONFIGURATION_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = (CONFIGURATION_OUTPUT_DIR / artifact_id / file_name).resolve()
        if not output_path.is_relative_to(CONFIGURATION_OUTPUT_DIR.resolve()):
            raise ValueError("Configuration path must remain inside the output directory")

        output_path.parent.mkdir(parents=True, exist_ok=False)
        serialized = manifest.encode()
        output_path.write_bytes(serialized)
    except OSError as error:
        raise ConfigurationPersistenceError(f"Unable to save configuration: {error}") from error
    digest = manifest_checksum.removeprefix("sha256:")
    configuration_file = ConfigurationFile(
        type=deployable.type,
        file_name=file_name,
        format=deployable.format,
        path=f"/configs/{artifact_id}/{file_name}",
        uri=artifact_uri("configuration", artifact_id, file_name),
        size=len(serialized),
        sha256=digest,
    )
    artifact = ConfigurationArtifactRecord(
        artifact_id=artifact_id,
        deployable_configuration=deployable,
        configuration_file=configuration_file,
        created_at=datetime.now(UTC),
    )
    register_artifacts(
        output_path.parent,
        owner_type="configuration",
        owner_id=artifact_id,
        source_version=(deployable.content.get("officialGuide", {}).get("source") or {}).get("commit"),
        configuration_ids=[artifact_id],
        retention_class="configuration",
    )
    _artifact_dao().create(artifact)
    return SaveResponse(configuration_file=configuration_file, artifact=artifact)


def get_configuration_artifact(artifact_id: str) -> ConfigurationArtifactRecord | None:
    try:
        normalized = str(UUID(artifact_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return _artifact_dao().get(normalized)


def list_configuration_artifacts() -> list[ConfigurationArtifactRecord]:
    return _artifact_dao().list()


def delete_configuration_artifact(artifact_id: str) -> bool:
    """Delete one saved artifact record and its generated manifest.

    Evaluation workflow cases embed an immutable copy of the deployable configuration, so
    deleting this catalog entry does not invalidate historical benchmark evidence.
    """
    artifact = get_configuration_artifact(artifact_id)
    if artifact is None:
        return False
    normalized = str(UUID(artifact_id))
    manifest_path = CONFIGURATION_OUTPUT_DIR / artifact.configuration_file.path.removeprefix("/configs/")
    if any(
        part.is_symlink()
        for part in (manifest_path, *manifest_path.parents)
        if part != CONFIGURATION_OUTPUT_DIR and part.is_relative_to(CONFIGURATION_OUTPUT_DIR)
    ):
        raise ValueError("Configuration manifest and owner symlinks are not supported")
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_relative_to(CONFIGURATION_OUTPUT_DIR.resolve()):
        raise ValueError("Configuration manifest path must remain inside the output directory")
    _artifact_dao().delete(normalized)
    manifest_path.unlink(missing_ok=True)
    if manifest_path.parent.name == normalized:
        (manifest_path.parent / "manifest.json").unlink(missing_ok=True)
        manifest_path.parent.rmdir()
    return True


def _artifact_dao():
    # Local import: llm_d_bench.db.models.configuration_artifact imports
    # ConfigurationArtifactRecord from this module's sibling models.py, so a
    # module-level import here would be fine (no cycle back into
    # service.py), but keeping it lazy matches the pattern used by
    # registry._dao() and keeps this module importable even before
    # the db package has finished initializing during test collection.
    from llm_d_bench.db.dao.configuration_artifact import (
        ConfigurationArtifactDao,
    )

    return ConfigurationArtifactDao()
