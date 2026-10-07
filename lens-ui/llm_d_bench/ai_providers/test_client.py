"""Tests for the provider-type-aware AI provider clients (llm_d_bench.ai_providers.client)."""

from __future__ import annotations

import pytest

from llm_d_bench.ai_providers.client import (
    AIProviderClientError,
    AnthropicCompatibleClient,
    OpenAICompatibleClient,
    build_client,
    client_for,
    get_client,
)
from llm_d_bench.ai_providers.contracts import AIProviderCreateRequest
from llm_d_bench.ai_providers.service import AIProviderService
from llm_d_bench.ai_providers.store import AIProviderStore


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._body


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def post(self, endpoint, *, json, headers):
        self.requests.append({"endpoint": endpoint, "json": json, "headers": headers})
        return self._responses[len(self.requests) - 1]


def test_client_for_selects_openai_by_default():
    client = client_for(base_url="https://api.openai.com/", model="gpt-4o-mini")
    assert isinstance(client, OpenAICompatibleClient)


def test_client_for_selects_anthropic_when_requested():
    client = client_for(base_url="https://api.anthropic.com/", model="claude-3-5-sonnet", provider_type="anthropic")
    assert isinstance(client, AnthropicCompatibleClient)


@pytest.mark.asyncio
async def test_client_rejects_private_provider_before_http_request(monkeypatch):
    requests = []
    monkeypatch.setattr(
        "llm_d_bench.ai_providers.client.httpx.AsyncClient",
        lambda **_kwargs: requests.append(True),
    )

    client = OpenAICompatibleClient(base_url="http://127.0.0.1:8080/", model="model")
    with pytest.raises(ValueError, match="public addresses"):
        await client.complete(system_prompt="sys", user_content="user")
    assert requests == []


@pytest.mark.asyncio
async def test_openai_client_sends_bearer_header_and_parses_content(monkeypatch):
    fake_client = _Client([_Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]})])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini", api_key="sk-abc")
    content = await client.complete(system_prompt="sys", user_content="user")

    assert content == '{"ok":true}'
    assert fake_client.requests[0]["headers"]["Authorization"] == "Bearer sk-abc"
    assert fake_client.requests[0]["endpoint"] == "https://api.openai.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_openai_client_normalizes_native_tool_calls(monkeypatch):
    fake_client = _Client(
        [
            _Response(
                body={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_cluster_overview",
                                            "arguments": '{"clusterId":"cluster-a"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini")
    turn = await client.complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"type": "function", "function": {"name": "get_cluster_overview", "parameters": {}}}],
    )

    assert turn.tool_calls[0].name == "get_cluster_overview"
    assert turn.tool_calls[0].arguments == {"clusterId": "cluster-a"}
    assert fake_client.requests[0]["json"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_client_normalizes_object_and_invalid_tool_arguments(monkeypatch, caplog):
    fake_client = _Client(
        [
            _Response(
                body={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "get_cluster_overview",
                                            "arguments": {"clusterId": "cluster-a"},
                                        },
                                    },
                                    {
                                        "id": "call-2",
                                        "function": {"name": "search_candidates", "arguments": "not JSON"},
                                    },
                                ],
                            }
                        }
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    turn = await OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini").complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[],
    )

    assert [call.arguments for call in turn.tool_calls] == [{"clusterId": "cluster-a"}, {}]
    assert "event=ai_tool_arguments_normalized" in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_failure_logs_safe_response_detail(monkeypatch, caplog):
    import httpx

    response = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_tool_history",
                "param": "messages",
                "message": "Missing tool result; api_key=secret-value",
            }
        },
        request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )
    fake_client = _Client([response])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)
    client = OpenAICompatibleClient(
        base_url="https://api.deepseek.com/",
        model="deepseek-chat",
        api_key="top-secret",
    )

    with pytest.raises(AIProviderClientError):
        await client.complete_tool_turn(
            system_prompt="must-not-be-logged",
            messages=[{"role": "user", "content": "private prompt"}],
            tools=[{"type": "function", "function": {"name": "search_candidates", "parameters": {}}}],
        )

    assert "invalid_tool_history" in caplog.text
    assert "messages" in caplog.text
    assert "top-secret" not in caplog.text
    assert "secret-value" not in caplog.text
    assert "private prompt" not in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_turn_retries_a_transient_transport_error(monkeypatch, caplog):
    import httpx

    class TransientClient(_Client):
        async def post(self, endpoint, *, json, headers):
            self.requests.append({"endpoint": endpoint, "json": json, "headers": headers})
            if len(self.requests) == 1:
                raise httpx.ReadTimeout("")
            return self._responses[0]

    fake_client = TransientClient([_Response(body={"choices": [{"message": {"content": "done"}}]})])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    turn = await OpenAICompatibleClient(
        base_url="https://api.deepseek.com/", model="deepseek-flash"
    ).complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[],
    )

    assert turn.content == "done"
    assert len(fake_client.requests) == 2
    assert "event=ai_tool_request_transport_failed" in caplog.text
    assert "error_type=ReadTimeout" in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_turn_reports_transport_error_type_when_retries_fail(monkeypatch, caplog):
    import httpx

    class ErroringClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectTimeout("")

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: ErroringClient())
    client = OpenAICompatibleClient(base_url="https://api.deepseek.com/", model="deepseek-flash")

    with pytest.raises(AIProviderClientError, match="ConnectTimeout"):
        await client.complete_tool_turn(
            system_prompt="sys",
            messages=[{"role": "user", "content": "plan"}],
            tools=[],
        )

    assert caplog.text.count("event=ai_tool_request_transport_failed") == 2


@pytest.mark.asyncio
async def test_openai_client_demotes_through_full_mode_ladder_on_structured_output_failures(monkeypatch):
    """An unrecognized host guesses "json_schema" first; on a
    response_format-shaped 400 it demotes to "json_object", then to "none",
    trying each rung at most once."""
    import httpx

    def _response_format_failure():
        failure_response = httpx.Response(
            400,
            json={"error": {"param": "response_format", "code": "unsupported"}},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        class Response400(_Response):
            def raise_for_status(self):
                raise httpx.HTTPStatusError("bad request", request=failure_response.request, response=failure_response)

        return Response400()

    class ClientWithRealFailure(_Client):
        async def post(self, endpoint, *, json, headers):
            self.requests.append({"endpoint": endpoint, "json": json, "headers": headers})
            if len(self.requests) < 3:
                return _response_format_failure()
            return _Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]})

    fake_client = ClientWithRealFailure([])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini")
    content = await client.complete(system_prompt="sys", user_content="user", json_schema={"type": "object"})

    assert content == '{"ok":true}'
    assert len(fake_client.requests) == 3
    assert fake_client.requests[0]["json"]["response_format"]["type"] == "json_schema"
    assert fake_client.requests[1]["json"]["response_format"]["type"] == "json_object"
    assert "response_format" not in fake_client.requests[2]["json"]


@pytest.mark.asyncio
async def test_openai_client_uses_configured_structured_output_mode_on_first_request(monkeypatch):
    """A client built with a known DeepSeek-style mode should send
    `json_object` (with a rendered example appended to the prompt) on its
    first request, without needing to fail with `json_schema` first."""
    fake_client = _Client([_Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]})])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(
        base_url="https://api.deepseek.com/",
        model="deepseek-chat",
        structured_output_mode="json_object",
    )
    content = await client.complete(
        system_prompt="sys",
        user_content="user",
        json_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )

    assert content == '{"ok":true}'
    assert len(fake_client.requests) == 1
    request_body = fake_client.requests[0]["json"]
    assert request_body["response_format"] == {"type": "json_object"}
    assert "json" in request_body["messages"][-1]["content"].lower()
    assert "answer" in request_body["messages"][-1]["content"]


def test_guess_structured_output_mode_recognizes_known_and_unknown_hosts():
    from llm_d_bench.ai_providers.client import guess_structured_output_mode

    assert guess_structured_output_mode("https://api.deepseek.com/") == "json_object"
    assert guess_structured_output_mode("https://api.deepseek.com/anthropic") == "json_object"
    assert guess_structured_output_mode("https://api.openai.com/") == "json_schema"


@pytest.mark.asyncio
async def test_anthropic_client_sends_x_api_key_header_and_parses_text_blocks(monkeypatch):
    fake_client = _Client([_Response(body={"content": [{"type": "text", "text": '{"ok":true}'}]})])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = AnthropicCompatibleClient(
        base_url="https://api.anthropic.com/", model="claude-3-5-sonnet", api_key="sk-ant-abc"
    )
    content = await client.complete(system_prompt="sys", user_content="user")

    assert content == '{"ok":true}'
    request = fake_client.requests[0]
    assert request["headers"]["x-api-key"] == "sk-ant-abc"
    assert "Authorization" not in request["headers"]
    assert request["endpoint"] == "https://api.anthropic.com/v1/messages"
    assert request["json"]["system"] == "sys"
    assert request["json"]["messages"] == [{"role": "user", "content": "user"}]


@pytest.mark.asyncio
async def test_anthropic_client_normalizes_native_tool_use(monkeypatch):
    fake_client = _Client(
        [
            _Response(
                body={
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "get_cluster_overview",
                            "input": {"clusterId": "cluster-a"},
                        }
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = AnthropicCompatibleClient(base_url="https://api.anthropic.com/", model="claude")
    turn = await client.complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"name": "get_cluster_overview", "description": "Get cluster", "input_schema": {}}],
    )

    assert turn.tool_calls[0].name == "get_cluster_overview"
    assert turn.tool_calls[0].arguments == {"clusterId": "cluster-a"}
    assert fake_client.requests[0]["json"]["tools"][0]["name"] == "get_cluster_overview"


@pytest.mark.asyncio
async def test_tool_turn_history_is_rendered_for_each_provider(monkeypatch):
    responses = [_Response(body={"choices": [{"message": {"content": "done"}}]})]
    openai_client = _Client(responses)
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: openai_client)
    history = [
        {"role": "user", "content": "plan"},
        {"role": "assistant", "content": "I will call a tool next.", "tool_calls": []},
        {"role": "user", "content": "Call it now."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "get_cluster_overview",
                    "arguments": {"clusterId": "cluster-a"},
                }
            ],
        },
        {"role": "tool", "results": [{"tool_call_id": "call-1", "content": '{"hardware":{}}'}]},
    ]
    await OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt").complete_tool_turn(
        system_prompt="sys",
        messages=history,
        tools=[],
    )
    openai_messages = openai_client.requests[0]["json"]["messages"]
    assert openai_messages[2] == {"role": "assistant", "content": "I will call a tool next."}
    assert openai_messages[-1] == {"role": "tool", "tool_call_id": "call-1", "content": '{"hardware":{}}'}

    anthropic_client = _Client([_Response(body={"content": [{"type": "text", "text": "done"}]})])
    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: anthropic_client)
    await AnthropicCompatibleClient(base_url="https://api.anthropic.com/", model="claude").complete_tool_turn(
        system_prompt="sys",
        messages=history,
        tools=[],
    )
    anthropic_messages = anthropic_client.requests[0]["json"]["messages"]
    assert anthropic_messages[-1]["role"] == "user"
    assert anthropic_messages[-1]["content"][0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_client_raises_ai_provider_client_error_on_unreachable_host(monkeypatch):
    import httpx

    async def public_addresses(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_bench.ai_providers.service._resolve_provider_addresses", public_addresses)

    class ErroringClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr("llm_d_bench.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: ErroringClient())

    client = OpenAICompatibleClient(base_url="https://unreachable.provider.org/", model="gpt-4o-mini")
    with pytest.raises(AIProviderClientError):
        await client.complete(system_prompt="sys", user_content="user")


def test_build_client_uses_saved_providers_type(tmp_path):
    service = AIProviderService(AIProviderStore(tmp_path))
    provider = service.create(
        AIProviderCreateRequest(
            name="Anthropic",
            baseUrl="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            apiKey="sk-ant-abc",
            providerType="anthropic",
        )
    )

    client = build_client(provider)

    assert isinstance(client, AnthropicCompatibleClient)
    assert client.api_key == "sk-ant-abc"


def test_get_client_returns_none_for_unset_or_missing_provider(monkeypatch, tmp_path):
    from llm_d_bench.ai_providers import service as ai_providers_service

    service = AIProviderService(AIProviderStore(tmp_path))
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: service)

    assert get_client(None) is None
    assert get_client("missing") is None


def test_get_client_returns_a_client_for_a_saved_provider(monkeypatch, tmp_path):
    from llm_d_bench.ai_providers import service as ai_providers_service

    service = AIProviderService(AIProviderStore(tmp_path))
    provider = service.create(
        AIProviderCreateRequest(name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-abc")
    )
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: service)

    client = get_client(provider.id)

    assert isinstance(client, OpenAICompatibleClient)
    assert client.model == "gpt-4o-mini"
