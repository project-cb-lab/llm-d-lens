"""Provider-type-aware HTTP clients for saved External AI Providers.

Any Agentic component that needs to call an external LLM should go through
``get_client(provider_id)`` (or ``build_client(provider)`` if it already holds
an ``AIProvider``) rather than talking to ``httpx`` directly. The concrete
transport -- request shape, auth headers, and response parsing -- differs by
``provider.provider_type``:

* ``"openai"``: ``POST {base_url}v1/chat/completions`` with a bearer auth
  header, and a ``response_format`` chosen per ``StructuredOutputMode`` (see
  below) for structured output.
* ``"anthropic"``: ``POST {base_url}v1/messages`` with ``x-api-key`` +
  ``anthropic-version`` headers. Anthropic's Messages API has no equivalent
  to OpenAI's ``response_format`` json_schema, so structured output is
  requested by instruction only.

Both clients expose the same ``complete()`` interface so callers stay
provider-agnostic.

Not every "OpenAI-compatible" provider actually implements OpenAI's newer,
strict ``response_format: json_schema``: DeepSeek, for example, only supports
the older, unconstrained ``response_format: json_object`` and 400s on
``json_schema``. Learning this by trial and error on every real planning
request wastes a full round of tokens, so ``guess_structured_output_mode()``
picks the right mode upfront from a small table of known hosts; ``complete()``
still keeps a demote-and-retry safety net (json_schema -> json_object -> no
response_format at all) for providers not in that table.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from llm_d_bench.ai_providers.contracts import DEFAULT_ANTHROPIC_VERSION, AIProvider

logger = logging.getLogger(__name__)

StructuredOutputMode = Literal["json_schema", "json_object", "none"]


class AIToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class AIToolTurn(BaseModel):
    content: str = ""
    tool_calls: list[AIToolCall] = Field(default_factory=list)


def _tool_arguments(value: Any) -> dict[str, Any]:
    """Normalize provider tool arguments like Lens Assistant does."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _openai_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "assistant":
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content") or None,
            }
            tool_calls = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])},
                }
                for call in message.get("tool_calls", [])
            ]
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            rendered.append(assistant_message)
        elif message["role"] == "tool":
            rendered.extend(
                {
                    "role": "tool",
                    "tool_call_id": result["tool_call_id"],
                    "content": result["content"],
                }
                for result in message.get("results", [])
            )
        else:
            rendered.append(message)
    return rendered


def _anthropic_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "assistant":
            content: list[dict[str, Any]] = []
            if message.get("content"):
                content.append({"type": "text", "text": message["content"]})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": call["arguments"],
                }
                for call in message.get("tool_calls", [])
            )
            rendered.append({"role": "assistant", "content": content})
        elif message["role"] == "tool":
            rendered.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result["tool_call_id"],
                            "content": result["content"],
                        }
                        for result in message.get("results", [])
                    ],
                }
            )
        else:
            rendered.append(message)
    return rendered


# Hosts known to reject OpenAI's strict `response_format: json_schema` but
# accept the older, unconstrained `response_format: json_object`. Extend this
# table as more "OpenAI-compatible" gateways are confirmed to only support the
# older mode -- unlisted hosts default to trying the modern `json_schema`
# first (see `guess_structured_output_mode`).
_JSON_OBJECT_ONLY_HOSTS = frozenset({"api.deepseek.com"})
_TOOL_TURN_TRANSPORT_ATTEMPTS = 2


def guess_structured_output_mode(base_url: str) -> StructuredOutputMode:
    """Best-effort, host-based guess of the structured-output style a saved
    OpenAI-compatible provider accepts, so the first real request already
    uses a format it supports instead of learning that via a failed, token-
    wasting request. Unknown hosts default to the modern, strict
    ``json_schema`` -- ``OpenAICompatibleClient.complete()``'s retry chain
    still recovers if this guess turns out to be wrong.
    """
    try:
        host = httpx.URL(base_url).host or ""
    except Exception:
        host = ""
    if host in _JSON_OBJECT_ONLY_HOSTS:
        return "json_object"
    return "json_schema"


def _example_value(schema: dict[str, Any]) -> Any:
    """Render one placeholder value for a JSON-Schema node, used to build a
    concrete example for providers (e.g. DeepSeek) whose weaker
    ``json_object`` mode isn't schema-aware and instead relies on an example
    in the prompt to shape its output."""
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties") or {}
        return {key: _example_value(value) for key, value in properties.items()}
    if schema_type == "array":
        item_schema = schema.get("items") or {}
        return [_example_value(item_schema)]
    if schema_type == "string":
        return schema.get("enum", ["string"])[0]
    if schema_type in ("number", "integer"):
        return 0
    if schema_type == "boolean":
        return True
    return None


def _json_object_prompt_hint(json_schema: dict[str, Any]) -> str:
    """DeepSeek's ``json_object`` mode docs require the word "json" and a
    concrete example in the prompt to reliably shape output -- a schema
    description alone (as used for real ``json_schema`` mode) isn't enough."""
    example = json.dumps(_example_value(json_schema))
    return (
        "\n\nRespond with a single JSON object only -- no other text, no markdown "
        f"code fences. Example of the exact shape (values are placeholders):\n{example}"
    )


def _bearer_auth_header(api_key: str) -> dict[str, str]:
    scheme = "Bearer"
    return {"Authorization": f"{scheme} {api_key}"} if api_key else {}


class AIProviderClientError(RuntimeError):
    """Raised when a provider request fails or returns an unusable response."""


def _safe_provider_error_detail(error: httpx.HTTPStatusError) -> str:
    response = error.response
    if response is None:
        return "response body unavailable"
    try:
        payload = response.json()
        detail = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(detail, dict):
            detail = {key: detail[key] for key in ("type", "code", "param", "message") if key in detail}
        rendered = json.dumps(detail, ensure_ascii=True)
    except (ValueError, TypeError):
        rendered = response.text
    rendered = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]?\s*[^\s,}\"]+", r"\1=[REDACTED]", rendered)
    return rendered[:2_000]


def _transport_error_detail(error: httpx.TransportError) -> str:
    """Return useful transport diagnostics without including request content."""
    cause = error.__cause__ or error.__context__
    reason = str(error) or (str(cause) if cause else "") or type(error).__name__
    return f"{type(error).__name__}: {reason}"


async def _post_tool_turn_with_retry(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    provider: str,
    model: str,
) -> httpx.Response:
    """Retry a single transient provider transport failure before falling back."""
    for attempt in range(1, _TOOL_TURN_TRANSPORT_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            return await client.post(endpoint, json=payload, headers=headers)
        except httpx.TransportError as error:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            detail = _transport_error_detail(error)
            logger.warning(
                "event=ai_tool_request_transport_failed provider=%s host=%s model=%s attempt=%s/%s "
                "elapsed_ms=%s error_type=%s detail=%s",
                provider,
                httpx.URL(endpoint).host,
                model,
                attempt,
                _TOOL_TURN_TRANSPORT_ATTEMPTS,
                elapsed_ms,
                type(error).__name__,
                detail,
            )
            if attempt == _TOOL_TURN_TRANSPORT_ATTEMPTS:
                raise
            await asyncio.sleep(0.25 * attempt)
    raise AssertionError("tool-turn transport retry loop exited unexpectedly")


class _TruncatedResponseError(AIProviderClientError):
    """Response was empty or cut off by the completion token budget.

    Distinct from other `AIProviderClientError`s so `OpenAICompatibleClient.complete()`
    knows it's safe/useful to retry with a larger `max_tokens` budget, unlike
    e.g. a malformed response body (which a bigger budget can't fix).
    """


class AIProviderClient(ABC):
    """A single-turn chat/completion client for one saved AI provider."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30,
        structured_output_mode: StructuredOutputMode = "json_schema",
    ) -> None:
        # Normalize so f"{self.base_url}v1/..." always inserts the required
        # path separator, even when the saved base URL has no trailing slash
        # (e.g. a bare "https://api.deepseek.com/anthropic").
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        # Only consulted by OpenAICompatibleClient -- see `guess_structured_output_mode`
        # for how this is picked for a saved provider.
        self.structured_output_mode = structured_output_mode

    @asynccontextmanager
    async def _http_client(self):
        from llm_d_bench.ai_providers.service import public_provider_client, validate_provider_endpoint

        await validate_provider_endpoint(self.base_url)
        async with public_provider_client(self.base_url, self.timeout_seconds) as client:
            yield client

    @abstractmethod
    async def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0,
    ) -> str:
        """Send one system+user turn and return the raw text response.

        ``json_schema``, when supported by the provider type, constrains the
        response to that JSON Schema; providers that don't support constrained
        output fall back to instruction-only JSON formatting.
        """
        raise NotImplementedError

    @abstractmethod
    async def complete_tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
        temperature: float = 0,
    ) -> AIToolTurn:
        """Run one native tool-calling turn and return a provider-neutral result."""
        raise NotImplementedError


class OpenAICompatibleClient(AIProviderClient):
    """Client for OpenAI-style ``POST /v1/chat/completions`` endpoints."""

    _MODE_ORDER: tuple[StructuredOutputMode, ...] = ("json_schema", "json_object", "none")
    # Reasoning-capable providers (e.g. DeepSeek) spend part of the completion
    # budget on hidden reasoning tokens before writing the actual answer, and
    # occasionally exhaust the whole budget that way, leaving an empty
    # `content`. Retry once per mode with a larger budget before giving up or
    # demoting -- capped so a persistently-empty response still fails fast.
    _EMPTY_CONTENT_MAX_TOKENS_CEILING = 16384

    @staticmethod
    def _is_structured_output_validation_failure(error: httpx.HTTPStatusError) -> bool:
        """True when a 400 looks like the provider rejected the requested
        ``response_format`` (some fields vary by vendor -- e.g. DeepSeek's
        error body doesn't match OpenAI's ``json_validate_failed``/
        ``param: response_format`` shape -- so this checks generically for
        the phrase anywhere in the error body instead of one fixed shape).
        """
        if error.response.status_code != 400:
            return False
        try:
            body = error.response.json()
        except ValueError:
            return False
        return "response_format" in json.dumps(body).lower()

    @staticmethod
    def _content(response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIProviderClientError("response has no choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        # `finish_reason == "length"` means the provider ran out of completion
        # budget -- for reasoning-capable providers this often happens *during*
        # hidden reasoning, before any answer text is written, so content can
        # be empty even though the request otherwise "succeeded" (200 OK).
        # Both cases are retriable with a larger `max_tokens` (see `complete`).
        if choice.get("finish_reason") == "length" or not isinstance(content, str) or not content.strip():
            raise _TruncatedResponseError("response was empty or truncated by the token budget")
        return content

    def _mode_ladder(self, json_schema: dict[str, Any] | None) -> tuple[StructuredOutputMode, ...]:
        """Modes to try in order, starting from this client's configured/guessed
        mode and demoting toward "none" only on a `response_format`-shaped 400."""
        if json_schema is None:
            return ("none",)
        start = (
            self._MODE_ORDER.index(self.structured_output_mode)
            if self.structured_output_mode in self._MODE_ORDER
            else 0
        )
        return self._MODE_ORDER[start:]

    def _build_payload(
        self,
        mode: StructuredOutputMode,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if mode == "json_schema" and json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_output", "strict": True, "schema": json_schema},
            }
            return payload
        if mode == "json_object" and json_schema is not None:
            # DeepSeek's `json_object` mode isn't schema-aware, so the schema
            # is instead rendered as a concrete example appended to the user
            # turn (see `_json_object_prompt_hint`).
            user_message = dict(messages[-1])
            user_message["content"] = user_message["content"] + _json_object_prompt_hint(json_schema)
            payload["messages"] = [*messages[:-1], user_message]
            payload["response_format"] = {"type": "json_object"}
        payload["max_tokens"] = max_tokens
        payload["temperature"] = temperature
        return payload

    async def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0,
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        headers = _bearer_auth_header(self.api_key)
        endpoint = f"{self.base_url}v1/chat/completions"
        modes = self._mode_ladder(json_schema)
        try:
            async with self._http_client() as client:
                for index, mode in enumerate(modes):
                    is_last_mode = index == len(modes) - 1
                    current_max_tokens = max_tokens
                    for attempt in range(2):
                        payload = self._build_payload(mode, messages, json_schema, current_max_tokens, temperature)
                        response = await client.post(endpoint, json=payload, headers=headers)
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as error:
                            if is_last_mode or not self._is_structured_output_validation_failure(error):
                                raise
                            break  # demote to the next mode
                        try:
                            return self._content(response.json())
                        except _TruncatedResponseError:
                            can_retry_bigger_budget = (
                                attempt == 0 and current_max_tokens < self._EMPTY_CONTENT_MAX_TOKENS_CEILING
                            )
                            if can_retry_bigger_budget:
                                current_max_tokens = min(current_max_tokens * 2, self._EMPTY_CONTENT_MAX_TOKENS_CEILING)
                                continue
                            if is_last_mode:
                                raise
                            break  # demote to the next mode
        except httpx.HTTPError as error:
            raise AIProviderClientError(f"OpenAI-compatible provider request failed: {error}") from error
        raise AIProviderClientError("OpenAI-compatible provider request failed: no response_format mode succeeded")

    async def complete_tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
        temperature: float = 0,
    ) -> AIToolTurn:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *_openai_tool_messages(messages)],
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        rendered_messages = payload["messages"]
        logger.info(
            "event=ai_tool_request provider=openai-compatible host=%s model=%s messages=%s roles=%s tools=%s "
            "tool_call_ids=%s payload_bytes=%s",
            httpx.URL(self.base_url).host,
            self.model,
            len(rendered_messages),
            [message.get("role") for message in rendered_messages],
            [tool.get("function", {}).get("name") for tool in tools],
            [
                call.get("id")
                for message in rendered_messages
                if message.get("role") == "assistant"
                for call in message.get("tool_calls", [])
            ],
            len(json.dumps(payload).encode("utf-8")),
        )
        try:
            async with self._http_client() as client:
                response = await _post_tool_turn_with_retry(
                    client,
                    endpoint=f"{self.base_url}v1/chat/completions",
                    payload=payload,
                    headers=_bearer_auth_header(self.api_key),
                    provider="openai-compatible",
                    model=self.model,
                )
                response.raise_for_status()
            message = response.json().get("choices", [{}])[0].get("message", {})
            calls = []
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function") or {}
                raw_arguments = function.get("arguments")
                arguments = _tool_arguments(raw_arguments)
                if raw_arguments not in (None, "", {}) and not arguments:
                    logger.warning(
                        "event=ai_tool_arguments_normalized provider=openai-compatible host=%s model=%s tool=%s",
                        httpx.URL(self.base_url).host,
                        self.model,
                        function.get("name"),
                    )
                calls.append(
                    AIToolCall(
                        id=str(raw_call.get("id") or ""),
                        name=str(function.get("name") or ""),
                        arguments=arguments,
                    )
                )
            return AIToolTurn(content=message.get("content") or "", tool_calls=calls)
        except httpx.HTTPStatusError as error:
            logger.warning(
                "event=ai_tool_request_failed provider=openai-compatible host=%s model=%s status=%s detail=%s",
                httpx.URL(self.base_url).host,
                self.model,
                error.response.status_code if error.response is not None else None,
                _safe_provider_error_detail(error),
            )
            raise AIProviderClientError(f"OpenAI-compatible provider request failed: {error}") from error
        except httpx.HTTPError as error:
            raise AIProviderClientError(
                f"OpenAI-compatible provider request failed: {_transport_error_detail(error)}"
            ) from error


class AnthropicCompatibleClient(AIProviderClient):
    """Client for Anthropic-style ``POST /v1/messages`` endpoints.

    The Messages API takes ``system`` as a separate top-level field (not a
    ``system``-role message) and has no ``response_format`` equivalent, so
    JSON output can only be requested via instruction, never enforced.
    """

    @staticmethod
    def _content(response: dict[str, Any]) -> str:
        blocks = response.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise AIProviderClientError("response has no content")
        text = "".join(
            block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text.strip():
            raise AIProviderClientError("response has no content")
        return text

    async def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0,
    ) -> str:
        headers = {"anthropic-version": DEFAULT_ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        payload = {
            "model": self.model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        endpoint = f"{self.base_url}v1/messages"
        try:
            async with self._http_client() as client:
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
            return self._content(response.json())
        except httpx.HTTPError as error:
            raise AIProviderClientError(f"Anthropic-compatible provider request failed: {error}") from error

    async def complete_tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
        temperature: float = 0,
    ) -> AIToolTurn:
        headers = {"anthropic-version": DEFAULT_ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        payload = {
            "model": self.model,
            "system": system_prompt,
            "messages": _anthropic_tool_messages(messages),
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            async with self._http_client() as client:
                response = await _post_tool_turn_with_retry(
                    client,
                    endpoint=f"{self.base_url}v1/messages",
                    payload=payload,
                    headers=headers,
                    provider="anthropic-compatible",
                    model=self.model,
                )
                response.raise_for_status()
            blocks = response.json().get("content") or []
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            calls = [
                AIToolCall(
                    id=str(block.get("id") or ""), name=str(block.get("name") or ""), arguments=block.get("input") or {}
                )
                for block in blocks
                if block.get("type") == "tool_use"
            ]
            return AIToolTurn(content=text, tool_calls=calls)
        except httpx.HTTPError as error:
            raise AIProviderClientError(
                f"Anthropic-compatible provider request failed: {_transport_error_detail(error)}"
            ) from error


def client_for(
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout_seconds: float = 30,
    provider_type: str = "openai",
    structured_output_mode: StructuredOutputMode | None = None,
) -> AIProviderClient:
    """Build the correct client implementation from raw connection settings.

    ``structured_output_mode`` is only consulted for ``"openai"`` providers;
    when omitted it's guessed from ``base_url`` (see
    ``guess_structured_output_mode``) so callers that don't care about this
    still get the right request shape on their first try.
    """
    if provider_type == "anthropic":
        return AnthropicCompatibleClient(
            base_url=base_url, model=model, api_key=api_key, timeout_seconds=timeout_seconds
        )
    return OpenAICompatibleClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        structured_output_mode=structured_output_mode or guess_structured_output_mode(base_url),
    )


def build_client(provider: AIProvider) -> AIProviderClient:
    """Build the correct client implementation for a saved provider's type."""
    return client_for(
        base_url=str(provider.base_url),
        model=provider.model,
        api_key=provider.api_key,
        timeout_seconds=provider.timeout_seconds,
        provider_type=provider.provider_type,
    )


def get_client(provider_id: str | None) -> AIProviderClient | None:
    """Look up a saved provider by ID and return its client, or ``None`` if unset/missing."""
    if not provider_id:
        return None
    from llm_d_bench.ai_providers.service import default_service

    provider = default_service().get(provider_id)
    if provider is None:
        return None
    return build_client(provider)
