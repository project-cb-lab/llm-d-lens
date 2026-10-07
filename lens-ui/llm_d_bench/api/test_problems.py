"""Problem-details handlers return a debuggable body for unhandled errors."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_d_bench.api.problems import install_problem_handlers


def _client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/boom")
    async def boom() -> dict:
        raise ValueError("something specific went wrong")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_error_returns_detail_and_request_id():
    response = _client().get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["title"] == "Internal server error"
    assert body["detail"] == "ValueError: something specific went wrong"
    assert body["requestId"]
