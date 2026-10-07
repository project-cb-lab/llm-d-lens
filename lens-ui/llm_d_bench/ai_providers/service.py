"""CRUD and connectivity testing for External AI Providers."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx

from llm_d_bench.ai_providers.contracts import (
    DEFAULT_ANTHROPIC_VERSION,
    AIProvider,
    AIProviderCreateRequest,
    AIProviderTestResult,
    AIProviderType,
    AIProviderUpdateRequest,
    utcnow,
)
from llm_d_bench.ai_providers.store import AIProviderStore, AIProviderStoreError, default_store


class AIProviderNameConflictError(Exception):
    """Raised when a provider name collides with an existing one."""


class UnsafeProviderEndpointError(ValueError):
    """Raised when a provider URL does not resolve exclusively to public HTTPS destinations."""

    def __init__(self) -> None:
        super().__init__("Provider URL must use HTTPS and resolve only to public addresses")


async def _resolve_provider_addresses(hostname: str, port: int) -> list[str]:
    results = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return [result[4][0] for result in results]


async def _public_provider_address(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise UnsafeProviderEndpointError
    decoded_path = parsed.path
    while unquote(decoded_path) != decoded_path:
        decoded_path = unquote(decoded_path)
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise UnsafeProviderEndpointError
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname.endswith((".localhost", ".local", ".internal", ".svc", ".test", ".example", ".invalid")):
        raise UnsafeProviderEndpointError
    try:
        port = parsed.port or 443
        try:
            addresses = [str(ipaddress.ip_address(hostname))]
        except ValueError:
            addresses = await asyncio.wait_for(_resolve_provider_addresses(hostname, port), timeout=5)
    except (OSError, TimeoutError, ValueError) as error:
        raise UnsafeProviderEndpointError from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeProviderEndpointError
    return addresses[0]


async def validate_provider_endpoint(base_url: str) -> None:
    await _public_provider_address(base_url)


class PublicProviderTransport(httpx.AsyncBaseTransport):
    def __init__(self, hostname: str) -> None:
        proxies = getproxies()
        proxy = None if proxy_bypass(hostname) else proxies.get("https") or proxies.get("all")
        self._trusted_proxy = bool(proxy and proxy == os.environ.get("LENS_TRUSTED_AI_PROVIDER_PROXY"))
        self._transport = httpx.AsyncHTTPTransport(proxy=proxy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        hostname = request.url.host
        address = await _public_provider_address(str(request.url))
        request.headers["host"] = request.url.netloc.decode("ascii")
        request.extensions["sni_hostname"] = hostname
        if not self._trusted_proxy:
            request.url = request.url.copy_with(host=address)
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def public_provider_client(base_url: str, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        transport=PublicProviderTransport(urlsplit(base_url).hostname or ""),
    )


class AIProviderService:
    def __init__(self, store: AIProviderStore | None = None) -> None:
        self._store = store or default_store()

    def list(self) -> list[AIProvider]:
        return self._store.list()

    def get(self, provider_id: str) -> AIProvider | None:
        return self._store.get(provider_id)

    def create(self, request: AIProviderCreateRequest) -> AIProvider:
        name = request.name.strip()
        if any(existing.name.strip().lower() == name.lower() for existing in self._store.list()):
            raise AIProviderNameConflictError(f"an AI provider named '{name}' already exists")
        provider = AIProvider(
            name=name,
            baseUrl=request.base_url,
            model=request.model,
            apiKey=request.api_key,
            providerType=request.provider_type,
            timeoutSeconds=request.timeout_seconds,
        )
        return self._store.create(provider)

    def update(self, provider_id: str, request: AIProviderUpdateRequest) -> AIProvider:
        provider = self._store.get(provider_id)
        if provider is None:
            raise AIProviderStoreError(f"AI provider not found: {provider_id}")
        if request.name is not None:
            name = request.name.strip()
            if any(
                existing.id != provider_id and existing.name.strip().lower() == name.lower()
                for existing in self._store.list()
            ):
                raise AIProviderNameConflictError(f"an AI provider named '{name}' already exists")
        updated = provider.model_copy(
            update={
                "name": request.name.strip() if request.name is not None else provider.name,
                "base_url": request.base_url if request.base_url is not None else provider.base_url,
                "model": request.model if request.model is not None else provider.model,
                "api_key": request.api_key if request.api_key is not None else provider.api_key,
                "provider_type": request.provider_type if request.provider_type is not None else provider.provider_type,
                "timeout_seconds": request.timeout_seconds
                if request.timeout_seconds is not None
                else provider.timeout_seconds,
                "updated_at": utcnow(),
            }
        )
        return self._store.save(updated)

    def delete(self, provider_id: str) -> None:
        if self._store.get(provider_id) is None:
            raise AIProviderStoreError(f"AI provider not found: {provider_id}")
        self._store.delete(provider_id)


async def check_connection(
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    provider_type: AIProviderType = "openai",
) -> AIProviderTestResult:
    """Probe a provider's model-list endpoint and report reachability.

    Best-effort: some OpenAI- or Anthropic-compatible gateways don't implement
    a model-list endpoint (or gate it separately from chat/message completions),
    so a non-2xx/unreachable result is reported as a failure with the
    underlying detail rather than silently treated as success -- the operator
    can still save the provider and rely on the real Agentic Deploy call path
    to confirm it works. For Anthropic-compatible endpoints specifically, a 404
    on the model-list route falls back to a minimal real ``POST /v1/messages``
    probe, since some gateways (e.g. DeepSeek's Anthropic-compatible endpoint)
    only implement message completion, not model listing.
    """
    try:
        await validate_provider_endpoint(base_url)
    except UnsafeProviderEndpointError:
        return AIProviderTestResult(
            success=False,
            message="Provider URL must use HTTPS and resolve only to public addresses.",
        )
    base_url = base_url if base_url.endswith("/") else f"{base_url}/"
    if provider_type == "anthropic":
        headers = {"anthropic-version": DEFAULT_ANTHROPIC_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    endpoint = f"{base_url}v1/models"
    started = time.monotonic()
    try:
        async with public_provider_client(base_url, timeout_seconds) as client:
            response = await client.get(endpoint, headers=headers)
        latency_ms = (time.monotonic() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        models = [str(item.get("id")) for item in payload.get("data") or [] if item.get("id")]
        model_found = model in models if models else None
        message = "Connected successfully."
        if models and not model_found:
            message = f"Connected, but model '{model}' was not found in the provider's model list."
        return AIProviderTestResult(
            success=True,
            message=message,
            modelFound=model_found,
            availableModels=models,
            latencyMs=round(latency_ms, 1),
        )
    except httpx.HTTPStatusError as error:
        latency_ms = (time.monotonic() - started) * 1000
        if provider_type == "anthropic" and error.response.status_code == 404:
            return await _probe_anthropic_messages(base_url, model, api_key, timeout_seconds)
        return AIProviderTestResult(
            success=False,
            message=f"Provider responded with an error: HTTP {error.response.status_code}.",
            latencyMs=round(latency_ms, 1),
        )
    except httpx.HTTPError as error:
        latency_ms = (time.monotonic() - started) * 1000
        return AIProviderTestResult(
            success=False,
            message=f"Could not reach the provider ({type(error).__name__}).",
            latencyMs=round(latency_ms, 1),
        )
    except UnsafeProviderEndpointError as error:
        return AIProviderTestResult(success=False, message=str(error))


async def _probe_anthropic_messages(
    base_url: str, model: str, api_key: str, timeout_seconds: float
) -> AIProviderTestResult:
    """Fall back for Anthropic-compatible gateways with no model-list route.

    Sends a minimal, essentially free ``POST /v1/messages`` request (a single
    output token) to confirm the endpoint, auth, and model name actually work
    end to end -- this is the same request shape the real planner call path
    uses, so a success here is a stronger signal than a model-list check would
    have been anyway.
    """
    try:
        await validate_provider_endpoint(base_url)
    except UnsafeProviderEndpointError:
        return AIProviderTestResult(
            success=False,
            message="Provider URL must use HTTPS and resolve only to public addresses.",
        )
    headers = {"anthropic-version": DEFAULT_ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    endpoint = f"{base_url}v1/messages"
    started = time.monotonic()
    try:
        async with public_provider_client(base_url, timeout_seconds) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
        latency_ms = (time.monotonic() - started) * 1000
        response.raise_for_status()
        return AIProviderTestResult(
            success=True,
            message=(
                "Connected successfully via a minimal message request "
                "(this endpoint has no model-list route, so the model list and "
                "model-found check were skipped)."
            ),
            latencyMs=round(latency_ms, 1),
        )
    except httpx.HTTPStatusError as error:
        latency_ms = (time.monotonic() - started) * 1000
        return AIProviderTestResult(
            success=False,
            message=(
                f"Provider responded with an error: HTTP {error.response.status_code} "
                "when sending a minimal message request (this endpoint has no model-list route)."
            ),
            latencyMs=round(latency_ms, 1),
        )
    except httpx.HTTPError as error:
        latency_ms = (time.monotonic() - started) * 1000
        return AIProviderTestResult(
            success=False,
            message=f"Could not reach the provider ({type(error).__name__}).",
            latencyMs=round(latency_ms, 1),
        )
    except UnsafeProviderEndpointError as error:
        return AIProviderTestResult(success=False, message=str(error))


_default_service: AIProviderService | None = None


def default_service() -> AIProviderService:
    global _default_service
    if _default_service is None:
        _default_service = AIProviderService()
    return _default_service
