"""Tests for the External AI Providers HTTP API (calls route handlers directly)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from llm_d_bench.ai_providers import router as ai_providers_router
from llm_d_bench.ai_providers.contracts import (
    AIProviderCreateRequest,
    AIProviderTestRequest,
    AIProviderUpdateRequest,
)
from llm_d_bench.ai_providers.service import AIProviderService, UnsafeProviderEndpointError
from llm_d_bench.ai_providers.store import AIProviderStore


@pytest.fixture()
def service(tmp_path, monkeypatch) -> AIProviderService:
    svc = AIProviderService(AIProviderStore(tmp_path))
    monkeypatch.setattr(ai_providers_router, "_service", svc)
    return svc


@pytest.fixture(autouse=True)
def public_provider_dns(monkeypatch):
    async def resolve(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", resolve)


@pytest.mark.asyncio
async def test_create_then_list_returns_masked_payloads(service):
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-secret"
        )
    )
    assert created["name"] == "OpenAI"
    assert created["hasApiKey"] is True
    assert "apiKey" not in created

    listing = await ai_providers_router.list_providers()
    assert [item["id"] for item in listing["items"]] == [created["id"]]


@pytest.mark.asyncio
async def test_create_duplicate_name_returns_409_problem(service):
    await ai_providers_router.create_provider(
        AIProviderCreateRequest(name="Dup", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    response = await ai_providers_router.create_provider(
        AIProviderCreateRequest(name="Dup", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_get_provider_404_for_unknown_id(service):
    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.get_provider("missing")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_update_provider_changes_fields(service):
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(name="Original", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    updated = await ai_providers_router.update_provider(created["id"], AIProviderUpdateRequest(model="gpt-4.1-mini"))

    assert updated["model"] == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_update_provider_404_for_unknown_id(service):
    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.update_provider("missing", AIProviderUpdateRequest(model="gpt-4.1-mini"))
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_provider_then_get_is_404(service):
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(name="Removable", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    response = await ai_providers_router.delete_provider(created["id"])
    assert response.status_code == 204

    with pytest.raises(HTTPException):
        await ai_providers_router.get_provider(created["id"])


@pytest.mark.asyncio
async def test_delete_provider_404_for_unknown_id(service):
    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.delete_provider("missing")
    assert excinfo.value.status_code == 404


class _Response:
    def __init__(self):
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"id": "gpt-4o-mini"}]}


class _Client:
    def __init__(self):
        self.last_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, *_args, **kwargs):
        self.last_headers = kwargs.get("headers")
        return _Response()


@pytest.mark.asyncio
async def test_test_draft_provider_reports_success(service, monkeypatch):
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: _Client())

    result = await ai_providers_router.test_draft_provider(
        AIProviderTestRequest(baseUrl="https://api.openai.com/", model="gpt-4o-mini", apiKey="sk-abc")
    )

    assert result["success"] is True
    assert result["modelFound"] is True


@pytest.mark.asyncio
async def test_test_saved_provider_reports_success(service, monkeypatch):
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: _Client())
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-abc")
    )

    result = await ai_providers_router.test_saved_provider(created["id"])

    assert result["success"] is True


@pytest.mark.asyncio
async def test_test_saved_provider_404_for_unknown_id(service):
    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.test_saved_provider("missing")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_test_draft_provider_uses_anthropic_headers_when_requested(service, monkeypatch):
    client = _Client()
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)

    result = await ai_providers_router.test_draft_provider(
        AIProviderTestRequest(
            baseUrl="https://api.anthropic.com/",
            model="claude-3-5-sonnet",
            apiKey="sk-ant-abc",
            providerType="anthropic",
        )
    )

    assert result["success"] is True
    assert client.last_headers.get("x-api-key") == "sk-ant-abc"
    assert "Authorization" not in client.last_headers


@pytest.mark.asyncio
async def test_test_saved_provider_uses_its_stored_provider_type(service, monkeypatch):
    client = _Client()
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="Anthropic",
            baseUrl="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            apiKey="sk-ant-abc",
            providerType="anthropic",
        )
    )

    result = await ai_providers_router.test_saved_provider(created["id"])

    assert result["success"] is True
    assert client.last_headers.get("x-api-key") == "sk-ant-abc"


class _ChatResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        self.text = text

    def json(self):
        return self._payload


class _ChatClient:
    def __init__(self, response=None):
        self._response = response or _ChatResponse()
        self.last_url = None
        self.last_json = None
        self.last_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def post(self, url, json=None, headers=None):
        self.last_url = url
        self.last_json = json
        self.last_headers = headers
        return self._response


@pytest.mark.asyncio
async def test_proxy_chat_completions_forwards_with_stored_key_and_default_model(service, monkeypatch):
    client = _ChatClient()
    monkeypatch.setattr("llm_d_bench.ai_providers.router.httpx.AsyncClient", lambda **_kwargs: client)
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-secret"
        )
    )

    result = await ai_providers_router.proxy_chat_completions(
        created["id"], {"messages": [{"role": "user", "content": "hi"}]}
    )

    assert result["choices"][0]["message"]["content"] == "hi"
    assert client.last_url == "https://api.openai.com/v1/chat/completions"
    assert client.last_headers["Authorization"] == "Bearer sk-secret"
    # The caller didn't specify a model, so the provider's saved model fills it in --
    # the real API key is never returned to the caller either way.
    assert client.last_json["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_proxy_chat_completions_rejects_dns_rebinding_during_request(service, monkeypatch):
    class RebindingClient(_ChatClient):
        async def post(self, *_args, **_kwargs):
            raise UnsafeProviderEndpointError()

    monkeypatch.setattr("llm_d_bench.ai_providers.router.httpx.AsyncClient", lambda **_kwargs: RebindingClient())
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.proxy_chat_completions(created["id"], {"messages": []})
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_proxy_chat_completions_uses_a_generous_fixed_timeout_not_the_providers_short_test_timeout(
    service, monkeypatch
):
    # provider.timeout_seconds is sized for the lightweight connectivity
    # probe (capped at 120s) -- a real chat completion must get a much
    # longer, fixed budget instead, or slow-but-successful completions
    # (e.g. a reasoning model) get aborted as a bogus 502 ReadTimeout.
    client = _ChatClient()
    captured_kwargs = {}

    def fake_async_client(**kwargs):
        captured_kwargs.update(kwargs)
        return client

    monkeypatch.setattr("llm_d_bench.ai_providers.router.httpx.AsyncClient", fake_async_client)
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-secret", timeoutSeconds=15
        )
    )

    await ai_providers_router.proxy_chat_completions(created["id"], {"messages": [{"role": "user", "content": "hi"}]})

    assert captured_kwargs["timeout"] == ai_providers_router.CHAT_COMPLETIONS_TIMEOUT_SECONDS
    assert captured_kwargs["timeout"] != 15


@pytest.mark.asyncio
async def test_proxy_chat_completions_404_for_unknown_id(service):
    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.proxy_chat_completions("missing", {"messages": []})
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_proxy_chat_completions_rejects_anthropic_providers(service):
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="Anthropic",
            baseUrl="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            apiKey="sk-ant-abc",
            providerType="anthropic",
        )
    )

    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.proxy_chat_completions(created["id"], {"messages": []})
    assert excinfo.value.status_code == 400


class _RaisingChatClient:
    """Simulates a connection failure where httpx's own exception carries no
    message at all (e.g. httpx.ConnectTimeout("")) -- the real-world case that used to surface as an unhelpful, empty
    "AI provider request failed: " detail."""

    def __init__(self, error):
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def post(self, url, json=None, headers=None):
        raise self._error


@pytest.mark.asyncio
async def test_proxy_chat_completions_reports_a_useful_message_even_when_the_underlying_error_is_empty(
    service, monkeypatch
):
    # str(httpx.ConnectTimeout("")) is "" -- assert the 502 detail still names
    # the exception type without disclosing the provider URL.
    client = _RaisingChatClient(httpx.ConnectTimeout(""))
    monkeypatch.setattr("llm_d_bench.ai_providers.router.httpx.AsyncClient", lambda **_kwargs: client)
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-secret"
        )
    )

    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.proxy_chat_completions(created["id"], {"messages": []})

    assert excinfo.value.status_code == 502
    assert "ConnectTimeout" in excinfo.value.detail
    assert "api.openai.com" not in excinfo.value.detail


@pytest.mark.asyncio
async def test_proxy_chat_completions_propagates_provider_error_status(service, monkeypatch):
    client = _ChatClient(response=_ChatResponse(status_code=400, text="bad request"))
    monkeypatch.setattr("llm_d_bench.ai_providers.router.httpx.AsyncClient", lambda **_kwargs: client)
    created = await ai_providers_router.create_provider(
        AIProviderCreateRequest(
            name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-secret"
        )
    )

    with pytest.raises(HTTPException) as excinfo:
        await ai_providers_router.proxy_chat_completions(created["id"], {"messages": []})
    assert excinfo.value.status_code == 400
