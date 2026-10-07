"""HTTP/OpenAPI compatibility for the Evaluation domain only."""

import importlib
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

router = importlib.import_module("llm_d_bench.evaluate.router")


def app():
    application = FastAPI()
    application.include_router(router.router)
    return application


def test_every_evaluation_json_response_has_a_named_contract():
    paths = app().openapi()["paths"]
    for path, methods in paths.items():
        for method, operation in methods.items():
            for code, response in operation["responses"].items():
                if code.startswith("2") and code != "204":
                    schema = response["content"]["application/json"]["schema"]
                    assert "$ref" in schema, (path, method, schema)
            assert "application/problem+json" in operation["responses"]["422"]["content"]


def test_list_response_preserves_legacy_fields_and_explicit_null(monkeypatch):
    record = {
        "id": str(uuid4()),
        "kind": "benchmark",
        "status": "succeeded",
        "created_at": "2026-09-21T00:00:00Z",
        "error": None,
        "metrics": {"throughput_tps": 0},
        "legacy_extension": {"value": 12},
    }
    monkeypatch.setattr(router, "_records", lambda kind: [record])
    response = TestClient(app()).get("/api/v1/evaluate/runs")
    assert response.status_code == 200
    assert response.json() == {"items": [record]}


def test_missing_benchmark_and_invalid_input_keep_http_status(monkeypatch):
    monkeypatch.setattr(router, "_get", lambda *_: None)
    client = TestClient(app())
    assert client.get(f"/api/v1/evaluate/runs/{uuid4()}").status_code == 404
    assert client.post("/api/v1/evaluate/runs", json={}).status_code == 422
