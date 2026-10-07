"""Shared validation for immutable Configuration and deployment artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol

import yaml

from llm_d_bench.utils.artifact_store import register_artifacts
from llm_d_bench.utils.paths import storage_path

DEPLOYMENT_MANIFEST_DIR = storage_path("data", "artifacts", "deployment-manifests")


class ConfigurationLike(Protocol):
    content: dict[str, Any]
    checksum: str
    provider_ref: str
    provenance: dict[str, Any]


class ClusterSessionLike(Protocol):
    id: str
    server_id: str


def configuration_checksum(content: dict[str, Any]) -> str:
    """Return a canonical checksum for Configuration-owned content."""
    serialized = json.dumps(content, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def text_checksum(content: str) -> str:
    """Return a checksum over exact UTF-8 text bytes."""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def configuration_cluster_ref(configuration: ConfigurationLike) -> dict[str, Any]:
    """Return the required cluster reference from one deployable Configuration."""
    cluster_ref = configuration.provenance.get("cluster_ref")
    if not isinstance(cluster_ref, dict):
        raise ValueError("deployable configuration requires provenance.cluster_ref")
    required = ("id", "session_id")
    missing = [name for name in required if not isinstance(cluster_ref.get(name), str) or not cluster_ref[name]]
    if missing:
        raise ValueError(f"deployable configuration cluster_ref is missing: {', '.join(missing)}")
    return cluster_ref


def validate_configuration_cluster_binding(
    configuration: ConfigurationLike,
    session: ClusterSessionLike,
) -> dict[str, Any]:
    """Prove that a Configuration artifact belongs to the active cluster session."""
    cluster_ref = configuration_cluster_ref(configuration)
    if cluster_ref["id"] != session.server_id:
        raise ValueError("configuration artifact cluster does not match the active cluster session")
    if cluster_ref["session_id"] != session.id:
        raise ValueError("configuration artifact session does not match the active cluster session")
    return cluster_ref


def deployment_manifest(configuration: ConfigurationLike) -> tuple[str, str, dict[str, Any]]:
    """Validate and return the exact manifest and deployment contract in an artifact."""
    if configuration.checksum != configuration_checksum(configuration.content):
        raise ValueError("deployable configuration checksum does not match content")
    official = configuration.content.get("officialGuide")
    if not isinstance(official, dict):
        raise ValueError("deployable configuration requires content.officialGuide")
    manifest = official.get("renderedManifest")
    expected_checksum = official.get("manifestChecksum")
    deployment = official.get("deployment")
    if not isinstance(manifest, str) or not manifest.strip():
        raise ValueError("official Guide rendered manifest is required")
    if not isinstance(expected_checksum, str) or expected_checksum != text_checksum(manifest):
        raise ValueError("official Guide manifest checksum does not match rendered manifest")
    if not isinstance(deployment, dict):
        raise ValueError("official Guide deployment contract is required")
    _validate_manifest_contract(manifest, deployment)
    return manifest, expected_checksum, deployment


def materialize_configuration_manifest(
    configuration_content: dict[str, Any],
    output_root: Path,
    provider_ref: str,
) -> tuple[Path, str, dict[str, Any], dict[str, Any]]:
    """Write an exact Configuration manifest to a checksum-addressed controlled path."""
    manifest, expected_checksum, deployment, source = validate_configuration_manifest_content(
        configuration_content, provider_ref
    )
    digest = expected_checksum.removeprefix("sha256:")
    directory = (output_root / digest).resolve()
    root = output_root.resolve()
    if not directory.is_relative_to(root):
        raise ValueError("configuration manifest path is outside the controlled output directory")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.yaml"
    if manifest_path.is_file() and manifest_path.read_text(encoding="utf-8") != manifest:
        raise ValueError("checksum-addressed configuration manifest content drift detected")
    if not manifest_path.is_file():
        temporary = directory / f"manifest.{os.getpid()}.tmp"
        temporary.write_text(manifest, encoding="utf-8")
        os.replace(temporary, manifest_path)
    register_artifacts(
        directory,
        owner_type="deployment-manifest",
        owner_id=digest,
        source_version=source.get("commit"),
        retention_class="configuration",
    )
    return manifest_path, expected_checksum, deployment, source


def validate_configuration_manifest_content(
    configuration_content: dict[str, Any], provider_ref: str
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Validate a provider-bound immutable manifest bundle without writing files."""
    official = configuration_content.get("officialGuide")
    if not isinstance(official, dict):
        raise ValueError("configuration content requires officialGuide")
    manifest = official.get("renderedManifest")
    expected_checksum = official.get("manifestChecksum")
    deployment = official.get("deployment")
    source = official.get("source")
    if not isinstance(manifest, str) or not manifest.strip():
        raise ValueError("configuration content requires an official Guide rendered manifest")
    if not isinstance(expected_checksum, str) or expected_checksum != text_checksum(manifest):
        raise ValueError("configuration manifest checksum does not match its content")
    if not isinstance(deployment, dict):
        raise ValueError("configuration content requires an official Guide deployment contract")
    if not isinstance(source, dict):
        raise ValueError("configuration content requires an official Guide source")
    if source.get("guide") != provider_ref:
        raise ValueError("configuration Guide source does not match the deployment provider")
    _validate_manifest_contract(manifest, deployment)
    from llm_d_bench.configuration.manifest_facts import validate_manifest_facts

    validate_manifest_facts(configuration_content, manifest)
    from llm_d_bench.configuration.guide_settings import validate_configuration_extensions

    validate_configuration_extensions(configuration_content, manifest)
    return manifest, expected_checksum, deployment, source


def _validate_manifest_contract(manifest: str, deployment: dict[str, Any]) -> None:
    try:
        documents = [item for item in yaml.safe_load_all(manifest) if isinstance(item, dict)]
    except yaml.YAMLError as error:
        raise ValueError(f"official Guide rendered manifest is invalid YAML: {error}") from error
    if not documents:
        raise ValueError("official Guide rendered manifest contains no Kubernetes resources")
    if any(item.get("kind") == "Namespace" for item in documents):
        raise ValueError("deployment manifest must not create or select a fixed Namespace")
    fixed_namespace = next(
        (
            item.get("metadata", {}).get("namespace")
            for item in documents
            if isinstance(item.get("metadata"), dict) and item["metadata"].get("namespace")
        ),
        None,
    )
    if fixed_namespace:
        raise ValueError("deployment manifest must be namespace-neutral")

    readiness = deployment.get("readinessDeployments")
    endpoint = deployment.get("endpoint")
    if (
        not isinstance(readiness, list)
        or not readiness
        or any(not isinstance(name, str) or not name for name in readiness)
    ):
        raise ValueError("deployment contract requires readinessDeployments")
    if not isinstance(endpoint, dict):
        raise ValueError("deployment contract requires endpoint")
    service_name = endpoint.get("serviceName")
    service_port = endpoint.get("port")
    if not isinstance(service_name, str) or not service_name:
        raise ValueError("deployment endpoint requires serviceName")
    if isinstance(service_port, bool) or not isinstance(service_port, int) or service_port <= 0:
        raise ValueError("deployment endpoint requires a positive port")

    deployments = {
        item.get("metadata", {}).get("name")
        for item in documents
        if item.get("kind") == "Deployment" and isinstance(item.get("metadata"), dict)
    }
    missing_deployments = [name for name in readiness if name not in deployments]
    if missing_deployments:
        raise ValueError(
            "deployment readiness resources are absent from rendered manifest: " + ", ".join(missing_deployments)
        )
    service = next(
        (
            item
            for item in documents
            if item.get("kind") == "Service"
            and isinstance(item.get("metadata"), dict)
            and item["metadata"].get("name") == service_name
        ),
        None,
    )
    ports = service.get("spec", {}).get("ports", []) if isinstance(service, dict) else []
    if not any(isinstance(port, dict) and port.get("port") == service_port for port in ports):
        raise ValueError("deployment endpoint service and port are absent from rendered manifest")
