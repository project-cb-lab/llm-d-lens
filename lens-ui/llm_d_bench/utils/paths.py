"""Lens filesystem layout. Selection only: domains own lifecycle and cleanup."""

from __future__ import annotations

import os
from pathlib import Path


def storage_path(area: str, *parts: str) -> Path:
    """Resolve a controlled path using only Lens roots and XDG defaults.

    LENS_* overrides win over XDG defaults. No directories are created and no
    legacy data is moved on import. Use the explicit storage migration command.
    """
    home = Path.home()
    defaults = {
        "data": Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "lens",
        "cache": Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "lens",
        "log": Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "lens/logs",
    }
    if area in defaults:
        root = Path(os.environ.get(f"LENS_{area.upper()}_DIR") or defaults[area])
    elif area == "scratch":
        root = Path(os.environ.get("LENS_SCRATCH_DIR") or storage_path("cache", "tmp"))
    elif area == "runtime":
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        root = Path(
            os.environ.get("LENS_RUNTIME_DIR") or (Path(xdg) / "lens" if xdg else storage_path("scratch", "runtime"))
        )
    else:
        raise ValueError(f"Unknown storage area: {area}")
    root = root.expanduser().resolve()
    for part in parts:
        if Path(part).is_absolute() or ".." in Path(part).parts:
            raise ValueError("Storage path must be relative without parent traversal")
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Storage path escapes its root")
    return target


def prism_temp_root(*parts: str) -> Path:
    """Compatibility entry point for provider scratch directories."""
    root = storage_path("scratch")
    for part in parts:
        if Path(part).is_absolute() or ".." in Path(part).parts:
            raise ValueError("Scratch path must be relative")
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Scratch path escapes its root")
    return target
