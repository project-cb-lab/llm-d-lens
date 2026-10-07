# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""FastAPI routes for Prism configuration processing."""

import io
import json
import logging
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from llm_d_bench.auth.access import current_principal, filter_by_cluster, require_cluster_access
from llm_d_bench.core.exceptions import DomainError
from llm_d_bench.deploy.capabilities import deployment_capabilities

from .errors import ConfigurationError
from .models import (
    ConfigurationArtifactRecord,
    RenderRequest,
    RenderResponse,
    ResolveRequest,
    ResolveResponse,
    SaveRequest,
    SaveResponse,
)
from .service import (
    delete_configuration_artifact,
    get_configuration_artifact,
    list_configuration_artifacts,
    render_configuration,
    resolve_configurations,
    save_configuration,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/configurations", tags=["configuration"])


@contextmanager
def _configuration_errors(failure_message: str, client_errors: tuple[type[Exception], ...]) -> Iterator[None]:
    """Adapt legacy configuration failures; let shared domain problems propagate."""
    try:
        yield
    except DomainError:
        raise
    except client_errors as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logger.exception(failure_message)
        raise HTTPException(status_code=500, detail=failure_message) from error


@router.get(
    "/capabilities",
    summary="Get Lens Configuration capability metadata and provider catalog information.",
    description=(
        "Get Lens Configuration capability metadata and provider catalog information. "
        "Use this to discover supported configuration providers and schema versions."
    ),
    operation_id="get_configuration_capabilities",
)
async def capabilities() -> dict:
    return {"schema_version": "deployment-capabilities.v1", "providers": deployment_capabilities()}


@router.post(
    "/resolve",
    response_model=ResolveResponse,
    summary="Resolve source configurations into validated candidates.",
    description=(
        "Normalize the supplied source configurations for the requested target and return candidate configurations "
        "with validation errors and warnings. Use this before rendering a deployable configuration. Configuration "
        "errors return 400; unexpected failures return 500 with Unable to resolve configurations. Shared domain "
        "errors retain their status and code."
    ),
    operation_id="resolve_configurations",
)
async def resolve(request: ResolveRequest) -> ResolveResponse:
    with _configuration_errors("Unable to resolve configurations", (ConfigurationError,)):
        return await resolve_configurations(request)


@router.post(
    "/render",
    response_model=RenderResponse,
    summary="Render a candidate into a deployable configuration.",
    description=(
        "Render a candidate configuration using the supplied render options and return the deployable configuration "
        "and metadata. Use the save endpoint to publish the result. Configuration and value errors return 400; "
        "unexpected failures return 500 with Unable to render configuration. Shared domain errors retain their "
        "status and code."
    ),
    operation_id="render_configuration",
)
async def render(request: RenderRequest) -> RenderResponse:
    with _configuration_errors("Unable to render configuration", (ConfigurationError, ValueError)):
        return render_configuration(request)


@router.post(
    "/save",
    response_model=SaveResponse,
    summary="Publish a deployable configuration as a saved artifact.",
    description=(
        "Save the supplied deployable configuration and return its published artifact record and file metadata. "
        "Use the artifact id for subsequent deployment or evaluation requests. Configuration, filesystem and value "
        "errors return 400; unexpected failures return 500 with Unable to save configuration. Shared domain errors "
        "retain their status and code."
    ),
    operation_id="save_configuration",
)
async def save(request: SaveRequest) -> SaveResponse:
    with _configuration_errors("Unable to save configuration", (ConfigurationError, OSError, ValueError)):
        return save_configuration(request)


def _artifact_cluster_id(artifact: ConfigurationArtifactRecord) -> str | None:
    deployable = getattr(artifact, "deployable_configuration", None)
    provenance = getattr(deployable, "provenance", None)
    if isinstance(provenance, dict):
        return (provenance.get("cluster_ref") or {}).get("id")
    cluster_ref = getattr(provenance, "cluster_ref", None)
    return getattr(cluster_ref, "id", None)


@router.get(
    "/artifacts",
    response_model=list[ConfigurationArtifactRecord],
    summary="List published Lens configuration artifacts.",
    description=(
        "List published Lens configuration artifacts. "
        "Use this to discover artifact ids that can be deployed or benchmarked later."
    ),
    operation_id="list_configuration_artifacts",
)
async def list_artifacts(request: Request = None) -> list[ConfigurationArtifactRecord]:
    artifacts = list_configuration_artifacts()
    return filter_by_cluster(artifacts, _artifact_cluster_id, current_principal(request))


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ConfigurationArtifactRecord,
    summary=(
        "Get one published Lens configuration artifact by id, including the deployable configuration and saved file "
        "metadata."
    ),
    description=(
        "Get one published Lens configuration artifact by id, including the deployable configuration and saved file "
        "metadata."
    ),
    operation_id="get_configuration_artifact",
)
async def get_artifact(artifact_id: str, request: Request = None) -> ConfigurationArtifactRecord:
    artifact = get_configuration_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="configuration artifact not found")
    require_cluster_access(current_principal(request), _artifact_cluster_id(artifact))
    return artifact


@router.get(
    "/artifacts/{artifact_id}/manifest",
    summary="Download the rendered YAML manifest file for a published configuration artifact.",
    description=(
        "Download the rendered YAML manifest file for a published configuration artifact. "
        "Use this when you need the exact manifest text Lens saved."
    ),
    operation_id="get_configuration_artifact_manifest",
)
async def download_artifact_manifest(artifact_id: str, request: Request = None) -> Response:
    artifact = get_configuration_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="configuration artifact not found")
    require_cluster_access(current_principal(request), _artifact_cluster_id(artifact))
    manifest = (artifact.deployable_configuration.content.get("officialGuide") or {}).get("renderedManifest")
    if isinstance(manifest, str) and manifest.strip():
        filename = quote(artifact.configuration_file.file_name, safe="")
        return Response(
            manifest,
            media_type="application/yaml",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            },
        )
    from .service import CONFIGURATION_OUTPUT_DIR

    path = (CONFIGURATION_OUTPUT_DIR / artifact.configuration_file.path.removeprefix("/configs/")).resolve()
    if not path.is_relative_to(CONFIGURATION_OUTPUT_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="configuration manifest file not found")
    return FileResponse(
        Path(path),
        media_type="application/yaml",
        filename=artifact.configuration_file.file_name,
    )


@router.delete(
    "/artifacts/{artifact_id}",
    status_code=204,
    summary="Delete a published configuration artifact from Lens.",
    description=(
        "Delete a published configuration artifact from Lens. "
        "Use this when an obsolete or incorrect artifact should no longer be deployable."
    ),
    operation_id="delete_configuration_artifact",
)
async def delete_artifact(artifact_id: str, request: Request = None) -> None:
    artifact = get_configuration_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="configuration artifact not found")
    require_cluster_access(current_principal(request), _artifact_cluster_id(artifact))
    try:
        deleted = delete_configuration_artifact(artifact_id)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="configuration artifact not found")


@router.get(
    "/artifacts/{artifact_id}/bundle",
    summary="Download the saved deployment inputs as a ZIP archive.",
    description=(
        "Download a ZIP containing the published configuration, rendered model-server manifest, deployment bundle, "
        "Helm values, auxiliary resources, and calibration recipe when present. Older artifacts without a deployment "
        "bundle return 409 and must be regenerated."
    ),
    operation_id="get_configuration_artifact_bundle",
)
async def download_artifact_bundle(artifact_id: str, request: Request = None) -> Response:
    artifact = get_configuration_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="configuration artifact not found")
    require_cluster_access(current_principal(request), _artifact_cluster_id(artifact))
    official = artifact.deployable_configuration.content.get("officialGuide") or {}
    bundle = official.get("deploymentBundle")
    if not bundle:
        raise HTTPException(
            status_code=409,
            detail=(
                "This older configuration contains model-server YAML only. "
                "Regenerate it to capture the complete deployment inputs."
            ),
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("configuration.json", artifact.model_dump_json(indent=2))
        archive.writestr("modelserver.yaml", official["renderedManifest"])
        archive.writestr("deployment-bundle.json", json.dumps(bundle, indent=2))
        calibration = bundle.get("calibration")
        calibration_assets = (
            [calibration] if isinstance(calibration, dict) else (calibration if isinstance(calibration, list) else [])
        )
        for asset in [*bundle["helm"]["values"], *bundle["resources"], *calibration_assets]:
            # Persisted assets are validated on publication; retain a path guard for old records.
            if Path(asset["name"]).name != asset["name"] or asset["name"] in {".", ".."}:
                raise HTTPException(status_code=400, detail="Invalid deployment bundle filename")
            archive.writestr(asset["name"], asset["content"])
        archive.writestr(
            "README.txt",
            (
                "Saved deployment inputs. modelserver.yaml contains the model-server resources; "
                "router-effective.yaml contains the merged Helm values. deployment-bundle.json records the chart "
                "version, source commit and content checksums. Auxiliary YAML and the calibration recipe are "
                "included when needed. Namespace, storage provisioning and post-deployment calibration are "
                "performed by Prism; effective calibration values are recorded in deployment diagnostics.\n"
            ),
        )
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="configuration-bundle.zip"'},
    )
