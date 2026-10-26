"""Where llmd keeps local files.

``LENS_DATA_DIR`` and ``LENS_CACHE_DIR`` override the XDG defaults, so a Lens
host and the standalone CLI agree on locations. Nothing here is required for
correctness: the inventory of what an instance owns lives in the cluster.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _platform_default(kind: str) -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "lens"
        return base / ("cache" if kind == "cache" else "data")
    if kind == "cache":
        return Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "lens"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "lens"


def data_root() -> Path:
    return Path(os.environ.get("LENS_DATA_DIR") or _platform_default("data")).expanduser()


def cache_root() -> Path:
    return Path(os.environ.get("LENS_CACHE_DIR") or _platform_default("cache")).expanduser()


def state_dir(override: str | Path | None = None) -> Path:
    """Directory for registered sources and rollout logs."""
    chosen = override or os.environ.get("LLMD_STATE_DIR")
    return Path(chosen).expanduser().resolve() if chosen else (data_root() / "llmd").resolve()


def repo_cache_dir() -> Path:
    return (cache_root() / "repos").resolve()
