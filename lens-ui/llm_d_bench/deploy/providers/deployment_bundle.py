"""Validate and consume immutable deployment inputs saved by Guide planning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.utils.paths import prism_temp_root
from llm_d_bench.utils.shell import spawn

CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
_SCHEMA_VERSION = "guide-deployment-bundle.v1"
_SAFE_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class MaterializedDeploymentBundle:
    chart: str
    version: str
    release_name: str
    effective_values: Path
    resources: tuple[Path, ...]
    calibration: Path | None


def subprocess_bundle_runner(
    helm_path: Path,
    kubectl_path: Path,
    environment: dict[str, str] | None = None,
) -> CommandRunner:
    """Build the production command boundary for validated Helm and kubectl plans."""

    async def run(command: list[str]) -> tuple[int, str, str]:
        executable = helm_path if command[0] == "helm" else kubectl_path
        process = await spawn(
            [str(executable), *command[1:]],
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    return run


def text_checksum(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def validate_deployment_bundle(bundle: Any, guide: str, source_commit: str) -> dict[str, Any]:
    """Validate a deployment bundle and its binding to one immutable Guide source."""
    if not isinstance(bundle, dict):
        raise ValueError("Guide deployment bundle must be an object")
    if bundle.get("schemaVersion") != _SCHEMA_VERSION:
        raise ValueError(f"Guide deployment bundle schemaVersion must be {_SCHEMA_VERSION}")
    if bundle.get("guide") != guide:
        raise ValueError("Guide deployment bundle Guide does not match its configuration")
    if bundle.get("sourceCommit") != source_commit:
        raise ValueError("Guide deployment bundle source commit does not match its configuration")
    if not re.fullmatch(r"[0-9a-f]{40}", str(bundle.get("sourceCommit") or "")):
        raise ValueError("Guide deployment bundle source commit must be a full Git commit")

    helm = bundle.get("helm")
    if not isinstance(helm, dict):
        raise ValueError("Guide deployment bundle requires Helm metadata")
    for key in ("chart", "version", "releaseName"):
        if not isinstance(helm.get(key), str) or not helm[key].strip():
            raise ValueError(f"Guide deployment bundle Helm {key} is required")
    values = helm.get("values")
    resources = bundle.get("resources")
    if not isinstance(values, list) or not values:
        raise ValueError("Guide deployment bundle requires Helm values")
    if not isinstance(resources, list):
        raise ValueError("Guide deployment bundle resources must be a list")

    calibration_assets = _calibration_assets(bundle)
    all_assets = [*values, *resources, *calibration_assets]
    names: set[str] = set()
    for item in all_assets:
        name, content = _validate_asset(item)
        if name in names:
            raise ValueError("Guide deployment bundle asset names must be unique")
        names.add(name)
        if item in values:
            _yaml_mapping(content, f"Helm values asset {name}")
        elif item in resources:
            _namespace_neutral_resources(content, name)
    if values[-1].get("name") != "router-effective.yaml":
        raise ValueError("Guide deployment bundle must end with router-effective.yaml")
    if guide == "precise-prefix-cache-routing":
        if not calibration_assets:
            raise ValueError("Precise routing deployment bundle requires calibration input")
        if not resources:
            raise ValueError("Precise routing deployment bundle requires auxiliary resources")
    return bundle


def _calibration_assets(bundle: dict[str, Any]) -> list[Any]:
    """Calibration assets to write beside the install output.

    Accepts a single asset (legacy bundles) or a list (a recipe plus the Job
    templates it reads from its own directory).
    """
    calibration = bundle.get("calibration")
    if calibration is None:
        return []
    if isinstance(calibration, dict):
        return [calibration]
    if isinstance(calibration, list):
        return calibration
    raise ValueError("Guide deployment bundle calibration must be an asset or a list of assets")


def materialize_deployment_bundle(
    bundle: dict[str, Any],
    guide: str,
    source_commit: str,
    output_root: Path | None = None,
) -> MaterializedDeploymentBundle:
    """Write validated bundle assets to a checksum-addressed private directory."""
    validate_deployment_bundle(bundle, guide, source_commit)
    serialized = json.dumps(bundle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    root = (output_root or prism_temp_root("guide-deployment-bundles")).resolve()
    directory = (root / digest).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("Guide deployment bundle path is outside the controlled output directory")
    directory.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    calibration_assets = _calibration_assets(bundle)
    assets = [*bundle["helm"]["values"], *bundle["resources"], *calibration_assets]
    for asset in assets:
        path = directory / asset["name"]
        if path.is_file() and path.read_text(encoding="utf-8") != asset["content"]:
            raise ValueError("checksum-addressed Guide deployment bundle drift detected")
        if not path.is_file():
            path.write_text(asset["content"], encoding="utf-8")
        paths[asset["name"]] = path
    calibration_script = next(
        (paths[asset["name"]] for asset in calibration_assets if str(asset["name"]).endswith(".sh")),
        None,
    )
    return MaterializedDeploymentBundle(
        chart=bundle["helm"]["chart"],
        version=bundle["helm"]["version"],
        release_name=bundle["helm"]["releaseName"],
        effective_values=paths["router-effective.yaml"],
        resources=tuple(paths[item["name"]] for item in bundle["resources"]),
        calibration=calibration_script,
    )


async def install_deployment_bundle(
    bundle: dict[str, Any],
    *,
    guide: str,
    source_commit: str,
    namespace: str,
    command_runner: CommandRunner,
    output_root: Path | None = None,
    effective_values_content: str | None = None,
    effective_values_name: str = "router-profile-effective.yaml",
) -> dict[str, Any]:
    """Render and install only saved bundle inputs through an injectable command boundary."""
    materialized = materialize_deployment_bundle(bundle, guide, source_commit, output_root)
    effective_values = materialized.effective_values
    if effective_values_content is not None:
        if not _SAFE_ASSET_NAME.fullmatch(effective_values_name):
            raise ValueError("Derived Router values require a safe file name")
        _yaml_mapping(effective_values_content, "Derived Router values")
        effective_values = materialized.effective_values.parent / effective_values_name
        effective_values.write_text(effective_values_content, encoding="utf-8")
    helm_common = [
        materialized.release_name,
        materialized.chart,
        "--namespace",
        namespace,
        "--version",
        materialized.version,
        "--values",
        str(effective_values),
    ]
    status, rendered, stderr = await command_runner(["helm", "template", *helm_common])
    if status != 0:
        raise RuntimeError((stderr or rendered or "saved Router Helm render failed").strip())
    for resource in materialized.resources:
        status, stdout, stderr = await command_runner(
            [
                "kubectl",
                "apply",
                "--namespace",
                namespace,
                "--filename",
                str(resource),
            ]
        )
        if status != 0:
            raise RuntimeError((stderr or stdout or "saved Guide resource apply failed").strip())
    status, stdout, stderr = await command_runner(["helm", "upgrade", "--install", *helm_common])
    if status != 0:
        raise RuntimeError((stderr or stdout or "saved Router deployment failed").strip())
    status, rendered, stderr = await command_runner(
        [
            "helm",
            "get",
            "manifest",
            materialized.release_name,
            "--namespace",
            namespace,
        ]
    )
    if status != 0:
        raise RuntimeError((stderr or rendered or "installed Router manifest capture failed").strip())
    if not rendered.strip():
        raise RuntimeError("installed Router manifest capture returned empty output")
    effective_content = effective_values.read_text(encoding="utf-8")
    return {
        "router_effective_values": effective_content,
        "router_effective_values_checksum": text_checksum(effective_content),
        "router_rendered_manifest": rendered,
        "router_rendered_manifest_checksum": text_checksum(rendered),
        "router_values_path": str(effective_values),
        "router_chart": materialized.chart,
        "router_version": materialized.version,
        "router_release_name": materialized.release_name,
        **({"calibration_script_path": str(materialized.calibration)} if materialized.calibration else {}),
    }


def _validate_asset(asset: Any) -> tuple[str, str]:
    if not isinstance(asset, dict):
        raise ValueError("Guide deployment bundle assets must be objects")
    name, content, checksum = asset.get("name"), asset.get("content"), asset.get("checksum")
    if not isinstance(name, str) or not _SAFE_ASSET_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("Guide deployment bundle asset requires a safe file name")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"Guide deployment bundle asset {name} requires text content")
    if checksum != text_checksum(content):
        raise ValueError(f"Guide deployment bundle asset {name} checksum does not match its content")
    return name, content


def _yaml_mapping(content: str, label: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError(f"{label} must be valid YAML: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return parsed


def _namespace_neutral_resources(content: str, name: str) -> None:
    try:
        documents = list(yaml.safe_load_all(content))
    except yaml.YAMLError as error:
        raise ValueError(f"Guide deployment resource {name} must be valid YAML: {error}") from error
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise ValueError(f"Guide deployment resource {name} must contain YAML mappings")
    for document in documents:
        metadata = document.get("metadata")
        if document.get("kind") == "Namespace" or (isinstance(metadata, dict) and metadata.get("namespace")):
            raise ValueError(f"Guide deployment resource {name} must be namespace-neutral")
