"""Tests for the External AI Providers service (CRUD + connection test)."""

from __future__ import annotations

import httpx
import pytest

from llm_d_bench.ai_providers.contracts import AIProviderCreateRequest, AIProviderUpdateRequest
from llm_d_bench.ai_providers.service import (
    AIProviderNameConflictError,
    AIProviderService,
    PublicProviderTransport,
    _probe_anthropic_messages,
    check_connection,
)
from llm_d_bench.ai_providers.store import AIProviderStore, AIProviderStoreError


@pytest.fixture()
def service(tmp_path) -> AIProviderService:
    return AIProviderService(AIProviderStore(tmp_path))


@pytest.fixture(autouse=True)
def public_provider_dns(monkeypatch):
    async def resolve(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", resolve)


def test_create_lists_and_masks_the_api_key(service):
    provider = service.create(
        AIProviderCreateRequest(
            name="My OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-secret-value"
        )
    )

    payload = provider.api_payload()
    assert payload["hasApiKey"] is True
    assert payload["apiKeyPreview"] == "...alue"
    assert "apiKey" not in payload
    assert "api_key" not in payload

    listed = service.list()
    assert [item.id for item in listed] == [provider.id]


def test_create_rejects_duplicate_name(service):
    service.create(AIProviderCreateRequest(name="Dup", baseUrl="https://api.openai.com", model="gpt-4o-mini"))

    with pytest.raises(AIProviderNameConflictError):
        service.create(AIProviderCreateRequest(name="Dup", baseUrl="https://api.openai.com", model="gpt-4o-mini"))


def test_update_changes_only_supplied_fields(service):
    provider = service.create(
        AIProviderCreateRequest(name="Original", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-abc")
    )

    updated = service.update(provider.id, AIProviderUpdateRequest(model="gpt-4.1-mini"))

    assert updated.name == "Original"
    assert updated.model == "gpt-4.1-mini"
    assert updated.api_key == "sk-abc"


def test_update_can_clear_the_api_key_with_an_explicit_empty_string(service):
    provider = service.create(
        AIProviderCreateRequest(name="Original", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-abc")
    )

    updated = service.update(provider.id, AIProviderUpdateRequest(apiKey=""))

    assert updated.api_key == ""


def test_update_rejects_rename_to_an_existing_name(service):
    service.create(AIProviderCreateRequest(name="First", baseUrl="https://api.openai.com", model="gpt-4o-mini"))
    second = service.create(
        AIProviderCreateRequest(name="Second", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    with pytest.raises(AIProviderNameConflictError):
        service.update(second.id, AIProviderUpdateRequest(name="First"))


def test_update_unknown_provider_raises(service):
    with pytest.raises(AIProviderStoreError):
        service.update("missing", AIProviderUpdateRequest(model="gpt-4.1-mini"))


def test_delete_removes_the_provider(service):
    provider = service.create(
        AIProviderCreateRequest(name="Removable", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    service.delete(provider.id)

    assert service.get(provider.id) is None


def test_delete_unknown_provider_raises(service):
    with pytest.raises(AIProviderStoreError):
        service.delete("missing")


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._body


class _Client:
    def __init__(self, response=None, error=None, post_response=None, post_error=None):
        self._response = response or _Response()
        self._error = error
        self._post_response = post_response
        self._post_error = post_error
        self.last_headers = None
        self.last_get_url = None
        self.last_post_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, url, **kwargs):
        self.last_get_url = url
        self.last_headers = kwargs.get("headers")
        if self._error is not None:
            raise self._error
        return self._response

    async def post(self, *_args, **kwargs):
        self.last_headers = kwargs.get("headers")
        self.last_post_json = kwargs.get("json")
        if self._post_error is not None:
            raise self._post_error
        return self._post_response or _Response()


@pytest.mark.asyncio
async def test_test_connection_reports_success_and_model_found(monkeypatch):
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: _Client())

    result = await check_connection("https://api.openai.com/", "gpt-4o-mini", "sk-abc", 30)

    assert result.success is True
    assert result.model_found is True
    assert "gpt-4o-mini" in result.available_models


@pytest.mark.asyncio
async def test_test_connection_accepts_versioned_provider_path(monkeypatch):
    client = _Client()
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)

    result = await check_connection("https://api.openai.com/openai/v1.0", "gpt-4o-mini", "", 30)

    assert result.success is True
    assert client.last_get_url == "https://api.openai.com/openai/v1.0/v1/models"


@pytest.mark.asyncio
async def test_provider_transport_pins_public_address_and_rejects_dns_rebinding(monkeypatch):
    requests = []

    class RecordingTransport:
        async def handle_async_request(self, request):
            requests.append(request)
            return httpx.Response(200, request=request)

        async def aclose(self):
            pass

    monkeypatch.setattr("llm_d_bench.ai_providers.service.getproxies", lambda: {})
    transport = PublicProviderTransport("api.openai.com")
    transport._transport = RecordingTransport()

    async def public_addresses(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", public_addresses)
    await transport.handle_async_request(httpx.Request("POST", "https://api.openai.com/openai/v1.0/v1/models"))
    assert requests[0].url == httpx.URL("https://8.8.8.8/openai/v1.0/v1/models")
    assert requests[0].headers["host"] == "api.openai.com"
    assert requests[0].extensions["sni_hostname"] == "api.openai.com"

    async def private_addresses(_hostname, _port):
        return ["127.0.0.1"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", private_addresses)
    with pytest.raises(ValueError):
        await transport.handle_async_request(httpx.Request("POST", "https://api.openai.com/v1/models"))
    assert len(requests) == 1


def test_provider_transport_preserves_configured_https_proxy(monkeypatch):
    selected = {}
    monkeypatch.setattr("llm_d_bench.ai_providers.service.getproxies", lambda: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr("llm_d_bench.ai_providers.service.proxy_bypass", lambda _hostname: False)
    monkeypatch.setattr(
        "llm_d_bench.ai_providers.service.httpx.AsyncHTTPTransport",
        lambda **kwargs: selected.update(kwargs),
    )

    PublicProviderTransport("api.openai.com")

    assert selected["proxy"] == "http://proxy.example:8080"


@pytest.mark.asyncio
async def test_trusted_provider_proxy_uses_hostname_after_public_validation(monkeypatch):
    requests = []

    class RecordingTransport:
        async def handle_async_request(self, request):
            requests.append(request)
            return httpx.Response(200, request=request)

        async def aclose(self):
            pass

    monkeypatch.setenv("LENS_TRUSTED_AI_PROVIDER_PROXY", "http://proxy.example:8080")
    monkeypatch.setattr("llm_d_bench.ai_providers.service.getproxies", lambda: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr("llm_d_bench.ai_providers.service.proxy_bypass", lambda _hostname: False)

    async def public_addresses(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", public_addresses)
    transport = PublicProviderTransport("api.openai.com")
    transport._transport = RecordingTransport()
    await transport.handle_async_request(httpx.Request("GET", "https://api.openai.com/v1/models"))
    assert requests[0].url == httpx.URL("https://api.openai.com/v1/models")
    assert requests[0].headers["host"] == "api.openai.com"
    assert requests[0].extensions["sni_hostname"] == "api.openai.com"

    async def private_addresses(_hostname, _port):
        return ["127.0.0.1"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", private_addresses)
    with pytest.raises(ValueError):
        await transport.handle_async_request(httpx.Request("GET", "https://api.openai.com/v1/models"))
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_proxy, bypass", [("http://other.example:8080", False), ("http://proxy.example:8080", True)])
async def test_trusted_provider_proxy_requires_matching_active_proxy(monkeypatch, configured_proxy, bypass):
    requests = []

    class RecordingTransport:
        async def handle_async_request(self, request):
            requests.append(request)
            return httpx.Response(200, request=request)

        async def aclose(self):
            pass

    monkeypatch.setenv("LENS_TRUSTED_AI_PROVIDER_PROXY", configured_proxy)
    monkeypatch.setattr("llm_d_bench.ai_providers.service.getproxies", lambda: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr("llm_d_bench.ai_providers.service.proxy_bypass", lambda _hostname: bypass)

    async def public_addresses(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", public_addresses)
    transport = PublicProviderTransport("api.openai.com")
    transport._transport = RecordingTransport()
    await transport.handle_async_request(httpx.Request("GET", "https://api.openai.com/v1/models"))
    assert requests[0].url == httpx.URL("https://8.8.8.8/v1/models")


@pytest.mark.asyncio
async def test_test_connection_reports_model_not_found(monkeypatch):
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: _Client())

    result = await check_connection("https://api.openai.com/", "does-not-exist", "sk-abc", 30)

    assert result.success is True
    assert result.model_found is False


@pytest.mark.asyncio
async def test_test_connection_reports_http_errors(monkeypatch):
    monkeypatch.setattr(
        "llm_d_bench.ai_providers.service.httpx.AsyncClient",
        lambda **_kwargs: _Client(response=_Response(status_code=401)),
    )

    result = await check_connection("https://api.openai.com/", "gpt-4o-mini", "bad-key", 30)

    assert result.success is False
    assert "401" in result.message


@pytest.mark.asyncio
async def test_test_connection_reports_unreachable_hosts(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "llm_d_bench.ai_providers.service.httpx.AsyncClient",
        lambda **_kwargs: _Client(error=httpx.ConnectError("connection refused")),
    )

    result = await check_connection("https://unreachable.example.org/", "gpt-4o-mini", "", 5)

    assert result.success is False
    assert "ConnectError" in result.message


@pytest.mark.asyncio
async def test_test_connection_rejects_private_destinations_without_request(monkeypatch):
    client_calls = []
    monkeypatch.setattr(
        "llm_d_bench.ai_providers.service.httpx.AsyncClient",
        lambda **_kwargs: client_calls.append(True),
    )

    result = await check_connection("https://127.0.0.1/", "model", "secret", 5)

    assert result.success is False
    assert "public addresses" in result.message
    assert client_calls == []


@pytest.mark.asyncio
async def test_test_connection_rejects_malformed_provider_urls_before_request(monkeypatch):
    client_calls = []
    monkeypatch.setattr(
        "llm_d_bench.ai_providers.service.httpx.AsyncClient",
        lambda **_kwargs: client_calls.append(True),
    )

    result = await check_connection("https://example.com/../../admin", "model", "secret", 5)

    assert result.success is False
    assert "public addresses" in result.message
    assert client_calls == []


@pytest.mark.asyncio
async def test_anthropic_fallback_rejects_malformed_provider_urls_before_request(monkeypatch):
    client_calls = []
    monkeypatch.setattr(
        "llm_d_bench.ai_providers.service.httpx.AsyncClient",
        lambda **_kwargs: client_calls.append(True),
    )

    result = await _probe_anthropic_messages("https://example.com/#fragment", "model", "secret", 5)

    assert result.success is False
    assert "public addresses" in result.message
    assert client_calls == []


@pytest.mark.asyncio
async def test_test_connection_defaults_to_openai_bearer_header(monkeypatch):
    client = _Client()
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)

    await check_connection("https://api.openai.com/", "gpt-4o-mini", "sk-abc", 30)

    assert client.last_headers.get("Authorization") == "Bearer sk-abc"
    assert "x-api-key" not in client.last_headers


@pytest.mark.asyncio
async def test_test_connection_uses_anthropic_headers_when_requested(monkeypatch):
    client = _Client()
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)

    result = await check_connection(
        "https://api.anthropic.com/", "claude-3-5-sonnet", "sk-ant-abc", 30, provider_type="anthropic"
    )

    assert result.success is True
    assert client.last_headers.get("x-api-key") == "sk-ant-abc"
    assert "Authorization" not in client.last_headers
    assert client.last_headers.get("anthropic-version") == "2023-06-01"


@pytest.mark.asyncio
async def test_test_connection_anthropic_404_falls_back_to_a_minimal_message_probe(monkeypatch):
    client = _Client(response=_Response(status_code=404), post_response=_Response(status_code=200, body={}))
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)

    result = await check_connection(
        "https://api.deepseek.com/anthropic/", "deepseek-v4-pro", "sk-abc", 30, provider_type="anthropic"
    )

    assert result.success is True
    assert "model-list route" in result.message
    assert client.last_headers.get("x-api-key") == "sk-abc"
    assert client.last_post_json["max_tokens"] == 1


@pytest.mark.asyncio
async def test_test_connection_anthropic_404_probe_reports_the_real_failure(monkeypatch):
    client = _Client(response=_Response(status_code=404), post_response=_Response(status_code=401))
    monkeypatch.setattr("llm_d_bench.ai_providers.service.httpx.AsyncClient", lambda **_kwargs: client)

    result = await check_connection(
        "https://api.deepseek.com/anthropic/", "deepseek-v4-pro", "bad-key", 30, provider_type="anthropic"
    )

    assert result.success is False
    assert "401" in result.message


def test_create_persists_provider_type(service):
    provider = service.create(
        AIProviderCreateRequest(
            name="Anthropic Provider",
            baseUrl="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            providerType="anthropic",
        )
    )

    assert provider.provider_type == "anthropic"
    assert provider.api_payload()["providerType"] == "anthropic"


def test_create_defaults_provider_type_to_openai(service):
    provider = service.create(
        AIProviderCreateRequest(name="Default Type", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    assert provider.provider_type == "openai"


def test_update_can_change_provider_type(service):
    provider = service.create(
        AIProviderCreateRequest(name="Original", baseUrl="https://api.openai.com", model="gpt-4o-mini")
    )

    updated = service.update(provider.id, AIProviderUpdateRequest(providerType="anthropic"))

    assert updated.provider_type == "anthropic"
