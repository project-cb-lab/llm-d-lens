"""Configuration routes retain their operation-specific HTTP error contracts."""

import importlib
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from llm_d_bench.configuration.errors import ConfigurationError

router = importlib.import_module("llm_d_bench.configuration.router")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,service,message",
    [
        ("resolve", "resolve_configurations", "Unable to resolve configurations"),
        ("render", "render_configuration", "Unable to render configuration"),
        ("save", "save_configuration", "Unable to save configuration"),
    ],
)
@pytest.mark.parametrize("error_type", [ConfigurationError, ValueError, OSError, RuntimeError])
async def test_operation_error_status_detail_and_cause(monkeypatch, operation, service, message, error_type):
    failure = error_type("specific failure")
    handler = AsyncMock if operation == "resolve" else Mock
    monkeypatch.setattr(router, service, handler(side_effect=failure))
    expected_client_error = (
        error_type is ConfigurationError
        or error_type is ValueError
        and operation in {"render", "save"}
        or error_type is OSError
        and operation == "save"
    )
    with pytest.raises(HTTPException) as raised:
        await getattr(router, operation)(object())
    assert raised.value.status_code == (400 if expected_client_error else 500)
    assert raised.value.detail == ("specific failure" if expected_client_error else message)
    assert raised.value.__cause__ is failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,service",
    [("resolve", "resolve_configurations"), ("render", "render_configuration"), ("save", "save_configuration")],
)
async def test_operation_returns_service_result(monkeypatch, operation, service):
    result = object()
    handler = AsyncMock if operation == "resolve" else Mock
    monkeypatch.setattr(router, service, handler(return_value=result))
    assert await getattr(router, operation)(object()) is result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,service",
    [("resolve", "resolve_configurations"), ("render", "render_configuration"), ("save", "save_configuration")],
)
async def test_domain_errors_reach_problem_handler_unchanged(monkeypatch, operation, service):
    from llm_d_bench.core.exceptions import ConflictError

    failure = ConflictError("resource conflict")
    failure.retryable = True
    handler = AsyncMock if operation == "resolve" else Mock
    monkeypatch.setattr(router, service, handler(side_effect=failure))
    with pytest.raises(ConflictError) as raised:
        await getattr(router, operation)(object())
    assert raised.value is failure
    assert raised.value.status_code == 409
    assert raised.value.code == "conflict"
    assert raised.value.retryable is True
