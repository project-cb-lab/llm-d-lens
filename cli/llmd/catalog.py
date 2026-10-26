"""The stacks a source holds, read from the checkout; no stack or model is compiled in.

A stack is a directory a team deploys with ``oc apply -k`` or ``oc apply -f``:

* a kustomize unit (``kustomization.yaml``) that no other unit includes: curvebender-tools'
  per-cluster overlays (``manifests/production/<model>/<cluster>``) are stacks, the ``base/``
  they include is not;
* any other directory or YAML file named explicitly, whose manifests are read as they are.

Listing leaves out what a team never applies as a stack: tooling directories, the files of
Helm charts, and what only a cluster administrator applies (``cluster-admin/`` and
``clusters/`` directories: model-cache volumes, network attachments, cluster-wide CRDs).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlio
from .errors import NotFoundError, UsageError
from .process import Runner
from .sources import Source

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
#: Top-level directories that hold tooling, never stacks.
TOOLING = {".github", "build", "docs", "proof-of-concept", "reports", "scripts", "tests"}
#: Directory names whose contents only a cluster administrator applies.
ADMIN_DIRECTORIES = {"cluster-admin", "clusters"}
#: Instance names become object names (``lens-instance-<name>``), so they stay short.
MAX_NAME = 40


@dataclass(frozen=True)
class Stack:
    id: str  # the path in the source, e.g. manifests/production/<model>/<cluster>
    kind: str  # kustomize | manifests
    source: Source

    @property
    def path(self) -> Path:
        return self.source.root / self.id

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "source": self.source.describe()}


def kustomization_file(directory: Path) -> Path | None:
    for name in KUSTOMIZATION_FILES:
        if (directory / name).is_file():
            return directory / name
    return None


def inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``; refuse absolute paths and traversal outside the source."""
    if not relative or Path(relative).is_absolute():
        raise UsageError(f"expected a path relative to the source: {relative!r}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise UsageError(f"path escapes the source checkout: {relative}")
    return resolved


def local_includes(directory: Path) -> list[Path]:
    """The local directories a kustomize unit includes as ``resources`` or ``components``."""
    kustomization = kustomization_file(directory)
    if kustomization is None:
        return []
    try:
        data = yamlio.load_file(kustomization) or {}
    except (OSError, UsageError):
        return []
    if not isinstance(data, dict):
        return []
    entries = [*(data.get("resources") or []), *(data.get("components") or [])]
    return [
        (directory / entry).resolve()
        for entry in entries
        if isinstance(entry, str) and "://" not in entry and (directory / entry).is_dir()
    ]


def uses_helm(directory: Path, seen: set[Path] | None = None) -> bool:
    """Whether a kustomize unit renders a Helm chart (``helmCharts``) itself or in a unit it includes;
    ``kustomize build`` then needs ``--enable-helm``."""
    seen = set() if seen is None else seen
    directory = directory.resolve()
    kustomization = kustomization_file(directory)
    if directory in seen or kustomization is None:
        return False
    seen.add(directory)
    try:
        data = yamlio.load_file(kustomization) or {}
    except (OSError, UsageError):
        return False
    if isinstance(data, dict) and data.get("helmCharts"):
        return True
    return any(uses_helm(include, seen) for include in local_includes(directory))


def manifest_documents(path: Path) -> list[dict[str, Any]] | None:
    """The Kubernetes objects in a YAML file, or None when it is not a manifest file (a kustomization,
    a Helm values file, anything without apiVersion and kind)."""
    if path.name in KUSTOMIZATION_FILES or path.name.endswith((".values.yaml", ".values.yml")):
        return None
    if path.name in ("values.yaml", "values.yml", "Chart.yaml"):
        return None
    try:
        documents = yamlio.load_documents(path.read_text(encoding="utf-8"), str(path))
    except (UsageError, UnicodeDecodeError, OSError):
        return None
    objects = [doc for doc in documents if doc.get("apiVersion") and doc.get("kind")]
    if not objects or {doc["kind"] for doc in objects} & {"Kustomization", "Component"}:
        return None
    return objects


def manifest_files(directory: Path) -> list[Path]:
    """Manifest files at or below ``directory``, leaving out nested kustomize units and Helm charts."""
    if directory.is_file():
        return [directory] if manifest_documents(directory) is not None else []
    found = []
    for path in sorted(directory.rglob("*")):
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue
        parents = [parent for parent in path.parents if parent.is_relative_to(directory)]
        if any(part.startswith(".") for part in path.relative_to(directory).parts):
            continue
        if any(parent != directory and kustomization_file(parent) for parent in parents):
            continue
        if any((parent / "Chart.yaml").is_file() for parent in parents):
            continue
        if manifest_documents(path) is not None:
            found.append(path)
    return found


def _tracked_files(source: Source, runner: Runner) -> list[str] | None:
    """The checkout's files (tracked and untracked, not ignored) when it is a Git work tree."""
    if not source.revision:
        return None
    listed = runner(
        ["git", "-C", str(source.root), "ls-files", "--cached", "--others", "--exclude-standard"], timeout=60
    )
    return listed.stdout.splitlines() if listed.ok else None


def _kustomize_units(source: Source, runner: Runner) -> set[str]:
    files = _tracked_files(source, runner)
    if files is None:
        files = [
            path.relative_to(source.root).as_posix()
            for name in KUSTOMIZATION_FILES
            for path in source.root.rglob(name)
        ]
    units = set()
    for line in files:
        path = Path(line)
        if path.name in KUSTOMIZATION_FILES and not any(part.startswith(".") for part in path.parts):
            units.add(path.parent.as_posix())
    return units


def _listed(source: Source, unit: str) -> bool:
    parts = Path(unit).parts
    if not parts or parts[0] in TOOLING or ADMIN_DIRECTORIES & set(parts):
        return False
    path = source.root / unit
    return not any(
        (parent / "Chart.yaml").is_file() for parent in [path, *path.parents] if parent != source.root
    )


def list_stacks(source: Source, runner: Runner, prefix: str = "") -> list[Stack]:
    """Every kustomize unit no other unit includes, outside tooling and administrator directories."""
    units = {unit for unit in _kustomize_units(source, runner) if unit != "." and _listed(source, unit)}
    paths = {(source.root / unit).resolve(): unit for unit in units}
    included = {paths[target] for path in paths for target in local_includes(path) if target in paths}
    prefix = prefix.strip("/")
    return [
        Stack(id=unit, kind="kustomize", source=source)
        for unit in sorted(units - included)
        if not prefix or unit == prefix or unit.startswith(prefix + "/")
    ]


def find_stack(source: Source, reference: str) -> Stack:
    """A stack by its path in the source: a kustomize unit, a directory of manifests, or one file."""
    reference = reference.strip().strip("/")
    if not reference:
        raise UsageError("no stack selected", hint="Run `llmd stacks` to list the stacks of the source.")
    path = inside(source.root, reference)
    identifier = path.relative_to(source.root.resolve()).as_posix()
    if path.is_dir() and kustomization_file(path):
        return Stack(id=identifier, kind="kustomize", source=source)
    if path.exists() and manifest_files(path):
        return Stack(id=identifier, kind="manifests", source=source)
    if path.exists():
        raise UsageError(f"{reference} holds no Kubernetes manifests")
    raise NotFoundError(
        f"stack {reference!r} not found in source {source.name}",
        hint="Run `llmd stacks` to list the stacks of the source.",
    )


def default_name(stack: Stack) -> str:
    """An instance name from the stack's last two path segments (``<model>-<cluster>``)."""
    tail = "-".join(stack.id.split("/")[-2:])
    name = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-") or "stack"
    if len(name) <= MAX_NAME:
        return name
    digest = hashlib.sha256(stack.id.encode()).hexdigest()[:8]
    return f"{name[: MAX_NAME - 9].rstrip('-')}-{digest}"
