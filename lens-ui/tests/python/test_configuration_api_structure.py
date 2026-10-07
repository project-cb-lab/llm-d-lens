# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.configuration import router
from llm_d_bench.configuration.app import app as compatibility_app


def test_shared_api_registers_configuration_router():
    client = TestClient(app)

    assert client.get("/api/health").status_code == 200
    assert client.post("/api/v1/configurations/resolve", json={}).status_code == 422
    assert client.post("/api/v1/configurations/render", json={}).status_code == 422
    assert client.post("/api/v1/configurations/save", json={}).status_code == 422


def test_configuration_router_owns_domain_prefix():
    assert router.prefix == "/api/v1/configurations"
    assert compatibility_app is app
