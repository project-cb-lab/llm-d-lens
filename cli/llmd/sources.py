"""Stack sources: a local checkout, or a Git URL pinned to a ref.

``--source`` takes a registered name (``llmd sources add``), a local path, or ``URL#REF``.
REF is a branch, a tag, a commit, or any ref the remote serves (``refs/pull/350/head``),
so a stack can be planned straight from upstream without a local checkout. Remote
checkouts are cached under ``$LENS_CACHE_DIR/repos``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths, yamlio
from .errors import CommandError, NotFoundError, UsageError
from .process import Runner, subprocess_runner

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SCP_URL = re.compile(r"^[\w.-]+@[\w.-]+:")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Source:
    name: str
    location: str
    root: Path
    kind: str  # local | git
    ref: str = ""
    revision: str = ""
    dirty: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "location": self.location,
            "kind": self.kind,
            "ref": self.ref,
            "revision": self.revision,
            "dirty": self.dirty,
        }


def is_git_url(value: str) -> bool:
    return "://" in value or bool(_SCP_URL.match(value))


def split_ref(location: str) -> tuple[str, str]:
    """``URL#REF`` -> (URL, REF). ``#`` is used because refs may contain ``/`` and ``@``."""
    base, _, ref = location.partition("#")
    return base, ref


class SourceRegistry:
    """Named sources in ``<state>/sources.yaml``."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "sources.yaml"

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        items = (yamlio.load_file(self.path) or {}).get("sources") or []
        return sorted((item for item in items if isinstance(item, dict) and "name" in item), key=_name)

    def get(self, name: str) -> dict[str, Any] | None:
        return next((item for item in self.entries() if item["name"] == name), None)

    def add(self, name: str, location: str, *, ref: str = "") -> dict[str, Any]:
        if not _NAME.match(name):
            raise UsageError(
                f"invalid source name {name!r}", hint="Use lowercase letters, digits, '.', '_', '-'."
            )
        location, inline_ref = split_ref(location)
        ref = ref or inline_ref
        if not is_git_url(location):
            resolved = Path(location).expanduser().resolve()
            if not resolved.is_dir():
                raise UsageError(f"source path does not exist: {location}")
            location = str(resolved)
        entry = {"name": name, "location": location, **({"ref": ref} if ref else {})}
        self._save([*(item for item in self.entries() if item["name"] != name), entry])
        return entry

    def remove(self, name: str) -> None:
        entries = self.entries()
        if not any(item["name"] == name for item in entries):
            raise NotFoundError(f"no registered source named {name!r}")
        self._save([item for item in entries if item["name"] != name])

    def _save(self, entries: list[dict[str, Any]]) -> None:
        yamlio.write_text_atomic(self.path, yamlio.dump({"sources": sorted(entries, key=_name)}))


def _name(entry: dict[str, Any]) -> str:
    return str(entry["name"])


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _git(runner: Runner, args: list[str], *, cwd: Path | None = None, timeout: float = 600) -> str:
    result = runner(["git", *args], cwd=str(cwd) if cwd else None, timeout=timeout)
    if not result.ok:
        raise CommandError(f"git {' '.join(args[:2])} failed: {result.message}")
    return result.stdout.strip()


def _checkout_dir(url: str, ref: str) -> Path:
    slug = _UNSAFE.sub("-", url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")).strip("-") or "repo"
    digest = hashlib.sha256(url.encode()).hexdigest()[:10]
    return paths.repo_cache_dir() / f"{slug}-{digest}" / (_UNSAFE.sub("-", ref).strip("-") or "default")


def _fetch(runner: Runner, url: str, ref: str, destination: Path) -> None:
    """Check out ``ref`` of ``url`` in ``destination``, fetching only that ref. A branch, a tag, a
    full commit, or any ref the remote advertises (``refs/pull/N/head``) works the same way."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    _git(runner, ["init", "--quiet", str(destination)])
    _git(runner, ["remote", "add", "origin", url], cwd=destination)
    _git(runner, ["fetch", "--quiet", "--depth", "1", "origin", ref or "HEAD"], cwd=destination)
    _git(runner, ["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=destination)


def _git_state(runner: Runner, root: Path) -> tuple[str, bool]:
    """(commit, dirty) for a checkout; ("", False) when it is not a Git work tree."""
    try:
        head = runner(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=30)
        if not head.ok:
            return "", False
        status = runner(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no", "--", "."], timeout=60
        )
    except CommandError:
        return "", False
    return head.stdout.strip(), bool(status.ok and status.stdout.strip())


def resolve_source(
    spec: str,
    *,
    registry: SourceRegistry | None = None,
    runner: Runner = subprocess_runner,
    refresh: bool = False,
) -> Source:
    """Resolve a registered name, a local path, or ``URL[#REF]`` into a checkout on disk."""
    if not spec:
        raise UsageError(
            "no stack source selected",
            hint="Pass --source PATH|URL#REF|NAME, or register one with `llmd sources add`.",
        )
    entry = registry.get(spec) if registry else None
    if entry:
        name, location, ref = spec, entry["location"], entry.get("ref", "")
    else:
        name = ""
        location, ref = split_ref(spec)
    if is_git_url(location):
        destination = _checkout_dir(location, ref)
        with _lock(str(destination)):
            if refresh or not (destination / ".git").exists():
                _fetch(runner, location, ref, destination)
        revision, dirty = _git_state(runner, destination)
        return Source(
            name=name or destination.parent.name,
            location=location,
            root=destination,
            kind="git",
            ref=ref,
            revision=revision,
            dirty=dirty,
        )
    root = Path(location).expanduser().resolve()
    if not root.is_dir():
        raise NotFoundError(f"stack source not found: {location}")
    if ref:
        raise UsageError(
            f"a local checkout is used as it is, so #{ref} cannot be applied to {location}",
            hint="Check the ref out in that directory, or pass the remote URL with #REF.",
        )
    revision, dirty = _git_state(runner, root)
    return Source(
        name=name or root.name,
        location=str(root),
        root=root,
        kind="local",
        ref=ref,
        revision=revision,
        dirty=dirty,
    )
