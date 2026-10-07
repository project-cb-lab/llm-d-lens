"""Shared RFC 7807 problem responses.

Lives outside the ``api`` package so feature routers can build problem
responses without importing the FastAPI application that mounts them.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


def problem(
    status: int,
    title: str,
    detail: object,
    code: str,
    extra: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    content: dict[str, object] = {
        "type": "about:blank",
        "status": status,
        "title": title,
        "detail": detail,
        "code": code,
    }
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status,
        content=content,
        media_type="application/problem+json",
        headers=headers,
    )
