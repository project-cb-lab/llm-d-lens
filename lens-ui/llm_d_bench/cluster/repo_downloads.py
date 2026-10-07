"""Download and cache llm-d / llm-d-benchmark source checkouts pinned to a
specific ref (branch, tag, or commit SHA) selected in the cluster-creation
wizard's "Software Versions" step.

Here we only need the requested ref's source tree fetched once and its path
remembered against the cluster record, so any other module (evaluate,
monitoring, ...) that later needs "the llm-d ref this cluster was configured
with" can resolve it from the cluster record instead of re-downloading.

Downloads are cached on disk under ``~/.cache/lens/repos/<repo>/<ref>``
(sanitized), keyed by ref, so re-selecting a previously downloaded ref is a
cheap no-op instead of a re-clone.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import run_sync

logger = logging.getLogger(__name__)

RepoName = Literal["llm-d", "llm-d-benchmark"]
DownloadState = Literal["idle", "downloading", "ready", "failed"]

REPO_URLS: dict[RepoName, str] = {
    "llm-d": "https://github.com/llm-d/llm-d.git",
    "llm-d-benchmark": "https://github.com/llm-d/llm-d-benchmark.git",
}


_CLONE_TIMEOUT_SECONDS = 300.0
_CHECKOUT_TIMEOUT_SECONDS = 120.0
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def cache_root() -> Path:
    """Root cache directory under LENS_CACHE_DIR."""
    return storage_path("cache", "repos")


def _slugify_ref(ref: str) -> str:
    return _UNSAFE_REF_CHARS.sub("-", ref).strip("-") or "default"


def repo_dir(repo: RepoName, ref: str) -> Path:
    """Directory a given ``repo``/``ref`` checkout is cached at (may not exist yet)."""
    return cache_root() / repo / _slugify_ref(ref)


def _is_checked_out(dest: Path) -> bool:
    return (dest / ".git").exists()


def _clone_at_ref(repo: RepoName, ref: str, dest: Path) -> None:
    """Blocking clone of ``repo`` at ``ref`` into ``dest``.

    Tries a fast shallow clone of ``ref`` as a branch/tag first; if that
    fails (most commonly because ``ref`` is a bare commit SHA, which
    ``git clone --branch`` cannot resolve), falls back to a full clone
    followed by an explicit checkout. Raises ``RuntimeError`` on failure.
    """
    url = REPO_URLS[repo]
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dest, ignore_errors=True)
    shallow = run_sync(
        ["git", "clone", "--quiet", "--depth", "1", "--branch", ref, url, str(dest)],
        timeout=_CLONE_TIMEOUT_SECONDS,
    )
    if shallow.returncode == 0 and _is_checked_out(dest):
        return
    shutil.rmtree(dest, ignore_errors=True)
    clone = run_sync(["git", "clone", "--quiet", url, str(dest)], timeout=_CLONE_TIMEOUT_SECONDS)
    if clone.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(clone.stderr.strip() or f"git clone of {url} failed")
    checkout = run_sync(
        ["git", "-C", str(dest), "checkout", "--quiet", "--detach", ref],
        timeout=_CHECKOUT_TIMEOUT_SECONDS,
    )
    if checkout.returncode != 0 or not _is_checked_out(dest):
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(checkout.stderr.strip() or f"git checkout of {ref} failed")


@dataclass
class DownloadStatus:
    repo: RepoName
    ref: str = ""
    state: DownloadState = "idle"
    path: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {"repo": self.repo, "ref": self.ref, "state": self.state, "path": self.path, "error": self.error}


_status_lock = Lock()
_status: dict[tuple[str, RepoName], DownloadStatus] = {}


def get_status(cluster_id: str, repo: RepoName) -> DownloadStatus:
    with _status_lock:
        existing = _status.get((cluster_id, repo))
        return existing if existing is not None else DownloadStatus(repo=repo)


def _set_status(cluster_id: str, repo: RepoName, **updates: object) -> None:
    with _status_lock:
        current = _status.get((cluster_id, repo)) or DownloadStatus(repo=repo)
        for key, value in updates.items():
            setattr(current, key, value)
        _status[(cluster_id, repo)] = current


async def ensure_downloaded(cluster_id: str, repo: RepoName, ref: str) -> Path:
    """Download ``repo`` at ``ref`` for ``cluster_id``, reusing an existing cache hit.

    Safe to call repeatedly/concurrently (e.g. the wizard step re-mounting):
    a ref that is already cached on disk is never re-cloned.
    """
    ref = ref.strip()
    dest = repo_dir(repo, ref)
    if _is_checked_out(dest):
        _set_status(cluster_id, repo, ref=ref, state="ready", path=str(dest), error=None)
        return dest
    _set_status(cluster_id, repo, ref=ref, state="downloading", path=None, error=None)
    try:
        await asyncio.to_thread(_clone_at_ref, repo, ref, dest)
    except Exception as error:
        logger.exception("event=repo_download_failed repo=%s ref=%s cluster_id=%s", repo, ref, cluster_id)
        _set_status(cluster_id, repo, ref=ref, state="failed", path=None, error=str(error))
        raise
    _set_status(cluster_id, repo, ref=ref, state="ready", path=str(dest), error=None)
    return dest
