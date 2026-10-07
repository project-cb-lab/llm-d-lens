"""Uniform Problem Details responses for Prism API modules."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from llm_d_bench.core.exceptions import DomainError
from llm_d_bench.utils.problems import problem

__all__ = ["install_problem_handlers", "problem"]

logger = logging.getLogger(__name__)


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_problem(_request: Request, error: DomainError) -> JSONResponse:
        return problem(error.status_code, "Domain request failed", str(error), error.code)

    @app.exception_handler(HTTPException)
    async def http_problem(_request: Request, error: HTTPException) -> JSONResponse:
        return problem(error.status_code, "Request failed", error.detail, "request_failed")

    @app.exception_handler(RequestValidationError)
    async def validation_problem(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = []
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            message = str(item.get("msg") or "invalid value")
            details.append(f"{location}: {message}" if location else message)
        return problem(422, "Request validation failed", "; ".join(details), "validation_failed")

    @app.exception_handler(Exception)
    async def unhandled_problem(request: Request, error: Exception) -> JSONResponse:
        # Surface a concrete, debuggable message instead of an opaque 500. The
        # request id ties the response to the full traceback in the server log.
        request_id = uuid.uuid4().hex
        logger.exception(
            "unhandled_error request_id=%s method=%s path=%s",
            request_id,
            request.method,
            request.url.path,
        )
        return problem(
            500,
            "Internal server error",
            f"{type(error).__name__}: {error}",
            "internal_error",
            extra={"requestId": request_id},
        )
