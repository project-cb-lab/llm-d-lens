"""Portable artifact inventories, independent of domain state and local roots.

Only explicitly public evidence belongs in a manifest. Credentials are never
registered. Domains choose lifecycle, provenance, file selection and retention.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

_LOCK = threading.RLock()
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RETENTION_CLASSES = frozenset({"configuration", "evidence", "diagnostic", "cache"})


def _file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Artifact path must be relative without traversal")
    candidate = root / path
    if any(
        parent.is_symlink()
        for parent in [candidate, *candidate.parents]
        if parent != root and parent.is_relative_to(root)
    ):
        raise ValueError("Artifact symlinks are not supported")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Artifact path escapes its root")
    return candidate


def artifact_uri(owner_type: str, owner_id: str, relative: str = "") -> str:
    if not _COMPONENT.fullmatch(owner_type) or not _COMPONENT.fullmatch(owner_id):
        raise ValueError("Invalid artifact owner")
    if relative and (Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("Invalid artifact relative path")
    return f"lens-artifact://{owner_type}/{owner_id}/" + quote(relative, safe="/._-")


def resolve_artifact(root: Path, manifest: Mapping[str, Any], uri: str) -> Path:
    """Resolve only a URI registered by this owner, never client-supplied paths."""
    parsed = urlsplit(uri)
    prefix = artifact_uri(manifest["owner_type"], manifest["owner_id"])
    if parsed.query or parsed.fragment or not uri.startswith(prefix):
        raise ValueError("Artifact URI does not belong to this manifest")
    if uri == manifest.get("uri"):
        relative = unquote(uri[len(prefix) :])
        if Path(relative).name != relative or not relative.endswith(".json"):
            raise ValueError("Invalid manifest URI")
        return _file(Path(root).resolve(), relative)
    entry = next((item for item in manifest["files"] if item["uri"] == uri), None)
    if entry is None:
        raise ValueError("Artifact is not registered")
    relative = unquote(uri[len(prefix) :])
    if relative != entry["path"]:
        raise ValueError("Artifact URI path mismatch")
    return _file(Path(root).resolve(), relative)


def register_artifacts(
    root: Path,
    *,
    owner_type: str,
    owner_id: str,
    source_version: Any = None,
    configuration_ids=(),
    retention_class: str = "evidence",
    truncated: bool = False,
    files: Mapping[str, Mapping[str, Any]] | None = None,
    status: str = "complete",
    manifest_name: str = "manifest.json",
) -> dict[str, Any]:
    """Hash actual payloads and atomically publish a portable v1 inventory.

    Call after writers close terminal files. For running diagnostics callers
    explicitly set status; size/checksum describe the snapshot read here.
    Unknown provenance is recorded rather than guessed from an installed tool.
    """
    artifact_uri(owner_type, owner_id)
    if retention_class not in RETENTION_CLASSES:
        raise ValueError("Unknown artifact retention class")
    if Path(manifest_name).name != manifest_name or not manifest_name.endswith(".json"):
        raise ValueError("Manifest name must be a JSON basename")
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Artifact root symlinks are not supported")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = _file(root, manifest_name)
    with _LOCK:
        previous = json.loads(target.read_text()) if target.exists() else {}
        if target.exists() and (
            not isinstance(previous, dict)
            or (previous.get("owner_type"), previous.get("owner_id")) != (owner_type, owner_id)
        ):
            raise ValueError("Existing artifact manifest has no matching owner")
        if files is None:
            files = {
                p.relative_to(root).as_posix(): {}
                for p in sorted(root.rglob("*"))
                if p.is_file()
                and p.name != "manifest.json"
                and not p.name.endswith((".manifest.json", ".tmp", ".lock"))
            }
        entries = []
        for relative, metadata in sorted(files.items()):
            path = _file(root, relative)
            if path == target:
                raise ValueError("Manifest cannot inventory itself")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            entries.append(
                {
                    "path": Path(relative).as_posix(),
                    "uri": artifact_uri(owner_type, owner_id, Path(relative).as_posix()),
                    "kind": metadata.get("kind", path.suffix.removeprefix(".") or "binary"),
                    "media_type": metadata.get("media_type")
                    or (
                        {".jsonl": "application/x-ndjson", ".log": "text/plain", ".yaml": "application/yaml"}.get(
                            path.suffix
                        )
                        or mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream"
                    ),
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                    "truncated": bool(metadata.get("truncated", truncated)),
                }
            )
        now = datetime.now(UTC).isoformat()
        manifest = {
            "schema_version": "artifact-manifest.v1",
            "owner_type": owner_type,
            "owner_id": owner_id,
            "uri": artifact_uri(owner_type, owner_id, manifest_name),
            "created_at": previous.get("created_at", now),
            "updated_at": now,
            "source_version": source_version if source_version is not None else {"status": "unknown"},
            "configuration_ids": sorted({str(value) for value in configuration_ids if value}),
            "retention_class": retention_class,
            "status": status,
            "truncated": bool(truncated or any(item["truncated"] for item in entries)),
            "files": entries,
        }
        temporary = target.with_name(f".{target.name}.{uuid4()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest
