"""Pin rendered Kubernetes workloads to one explicitly configured node."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.utils.artifacts import text_checksum

TARGET_NODE_ENV_VAR = "PRISM_K8S_TARGET_NODE"
_NODE_NAME_PATTERN = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*")
_POD_TEMPLATE_KINDS = {"DaemonSet", "Deployment", "Job", "ReplicaSet", "ReplicationController", "StatefulSet"}


def configured_target_node(environ: dict[str, str] | None = None) -> str | None:
    """Return and validate the configured Kubernetes node name."""
    target_node = (environ or os.environ).get(TARGET_NODE_ENV_VAR, "").strip()
    if not target_node:
        return None
    if len(target_node) > 253 or not _NODE_NAME_PATTERN.fullmatch(target_node):
        raise ValueError(f"{TARGET_NODE_ENV_VAR} must be a valid Kubernetes node name")
    return target_node


def pin_manifest_to_node(manifest: str, target_node: str) -> tuple[str, int]:
    """Add a hostname nodeSelector to every workload in a YAML manifest."""
    documents = list(yaml.safe_load_all(manifest))
    pinned = 0

    def pin_resource(resource: Any) -> None:
        nonlocal pinned
        if not isinstance(resource, dict):
            return
        if resource.get("kind") == "List":
            for item in resource.get("items") or []:
                pin_resource(item)
            return

        kind = resource.get("kind")
        spec = resource.get("spec")
        pod_spec: dict[str, Any] | None = None
        if kind == "Pod" and isinstance(spec, dict):
            pod_spec = spec
        elif kind in _POD_TEMPLATE_KINDS and isinstance(spec, dict):
            template = spec.get("template")
            if isinstance(template, dict) and isinstance(template.get("spec"), dict):
                pod_spec = template["spec"]
        elif kind == "CronJob" and isinstance(spec, dict):
            pod_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")

        if isinstance(pod_spec, dict):
            selector = pod_spec.setdefault("nodeSelector", {})
            if not isinstance(selector, dict):
                raise ValueError(f"{kind} nodeSelector must be a mapping")
            selector["kubernetes.io/hostname"] = target_node
            pinned += 1

    for document in documents:
        pin_resource(document)
    if not pinned:
        raise ValueError("deployment manifest contains no pinnable Kubernetes workloads")
    return yaml.safe_dump_all(documents, sort_keys=False), pinned


def write_pinned_manifest(manifest_ref: str, output_root: Path, target_node: str) -> tuple[str, str, int]:
    """Write a content-addressed pinned copy without mutating provider output."""
    manifest = Path(manifest_ref).read_text(encoding="utf-8")
    rendered, pinned = pin_manifest_to_node(manifest, target_node)
    checksum = text_checksum(rendered)
    directory = output_root.resolve() / checksum.removeprefix("sha256:")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.yaml"
    if path.is_file() and path.read_text(encoding="utf-8") != rendered:
        raise ValueError("checksum-addressed pinned deployment manifest drift detected")
    if not path.is_file():
        path.write_text(rendered, encoding="utf-8")
    from llm_d_bench.utils.artifact_store import register_artifacts

    register_artifacts(
        directory,
        owner_type="deployment-manifest",
        owner_id=checksum.removeprefix("sha256:"),
        retention_class="configuration",
    )
    return str(path), checksum, pinned
