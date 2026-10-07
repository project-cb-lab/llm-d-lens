"""Shared helpers for serving file downloads from the backend."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse


def ensure_within_root(path: str | Path, root: str | Path) -> Path:
    """Resolve *path* and require that it stays inside *root*.

    Guards against path traversal when a stored artifact path is used to
    resolve a download target.
    """
    resolved_root = Path(root).expanduser().resolve()
    resolved_path = Path(path).expanduser().resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError("path escapes the allowed root directory")
    return resolved_path


def file_download_response(
    path: str | Path,
    *,
    media_type: str,
    filename: str | None = None,
) -> FileResponse:
    """Return a FileResponse that triggers a browser download for *path*."""
    resolved = Path(path).expanduser().resolve()
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=filename or resolved.name,
        content_disposition_type="attachment",
    )
