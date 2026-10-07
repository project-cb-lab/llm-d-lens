"""HTTP API for the External AI Providers module: register/list/update/delete/test."""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from llm_d_bench.ai_providers.contracts import (
    AIProvider,
    AIProviderCreateRequest,
    AIProviderTestRequest,
    AIProviderUpdateRequest,
)
from llm_d_bench.ai_providers.service import (
    AIProviderNameConflictError,
    UnsafeProviderEndpointError,
    check_connection,
    default_service,
    public_provider_client,
    validate_provider_endpoint,
)
from llm_d_bench.ai_providers.store import AIProviderStoreError
from llm_d_bench.utils.problems import problem

router = APIRouter(prefix="/api/v1/ai-providers", tags=["ai-providers"])
logger = logging.getLogger(__name__)
_service = default_service()


def _payload(provider: AIProvider) -> dict:
    return provider.api_payload()


@router.get(
    "",
    summary="List the saved external AI provider connections Lens knows about.",
    description=(
        "List the saved external AI provider connections Lens knows about. Returned records mask stored API keys "
        "and expose only a safe preview."
    ),
    operation_id="list_ai_providers",
)
async def list_providers() -> dict:
    return {"items": [_payload(provider) for provider in _service.list()]}


@router.get(
    "/{provider_id}",
    summary="Get one saved external AI provider connection by id.",
    description=(
        "Get one saved external AI provider connection by id. The response masks the stored API key and includes "
        "only safe metadata."
    ),
    operation_id="get_ai_provider",
)
async def get_provider(provider_id: str) -> dict:
    provider = _service.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"AI provider not found: {provider_id}")
    return _payload(provider)


@router.post(
    "",
    status_code=201,
    summary="Create and persist a new external AI provider configuration.",
    description=(
        "Create and persist a new external AI provider configuration. Use this for durable provider definitions "
        "Lens may later reuse for agentic planning or other integrations."
    ),
    operation_id="create_ai_provider",
)
async def create_provider(request: AIProviderCreateRequest) -> dict:
    try:
        provider = _service.create(request)
    except AIProviderNameConflictError as error:
        return problem(409, "AI provider name already in use", str(error), "ai_provider_name_conflict")
    logger.info("event=ai_provider_created provider_id=%s name=%s", provider.id, provider.name)
    return _payload(provider)


@router.put(
    "/{provider_id}",
    summary="Update an existing external AI provider configuration by id.",
    description=(
        "Update an existing external AI provider configuration by id. Omit apiKey to keep the stored key unchanged, "
        "or pass an empty string to clear it."
    ),
    operation_id="update_ai_provider",
)
async def update_provider(provider_id: str, request: AIProviderUpdateRequest) -> dict:
    try:
        provider = _service.update(provider_id, request)
    except AIProviderStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AIProviderNameConflictError as error:
        return problem(409, "AI provider name already in use", str(error), "ai_provider_name_conflict")
    logger.info("event=ai_provider_updated provider_id=%s", provider.id)
    return _payload(provider)


@router.delete(
    "/{provider_id}",
    status_code=204,
    summary="Delete a saved external AI provider configuration.",
    description=(
        "Delete a saved external AI provider configuration. Use this when a provider should no longer be available "
        "to Lens."
    ),
    operation_id="delete_ai_provider",
)
async def delete_provider(provider_id: str):
    try:
        _service.delete(provider_id)
    except AIProviderStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    logger.info("event=ai_provider_deleted provider_id=%s", provider_id)
    return Response(status_code=204)


@router.post(
    "/test",
    summary="Test connectivity for a not-yet-saved external AI provider configuration.",
    description=(
        "Test connectivity for a not-yet-saved external AI provider configuration. "
        "Use this before creating or updating a durable provider entry."
    ),
    operation_id="test_ai_provider_draft_connection",
)
async def test_draft_provider(request: AIProviderTestRequest) -> dict:
    """Test connectivity for a not-yet-saved configuration (the create/edit form)."""
    result = await check_connection(
        str(request.base_url),
        request.model,
        request.api_key,
        request.timeout_seconds,
        request.provider_type,
    )
    return result.model_dump(mode="json", by_alias=True)


@router.post(
    "/{provider_id}/test",
    summary=("Test connectivity for an already-saved external AI provider by id, reusing its stored API key."),
    description=(
        "Test connectivity for an already-saved external AI provider by id, reusing its stored API key. "
        "Use this to validate an existing provider definition."
    ),
    operation_id="test_ai_provider_connection",
)
async def test_saved_provider(provider_id: str) -> dict:
    """Test connectivity for an already-saved provider, reusing its stored API key."""
    provider = _service.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"AI provider not found: {provider_id}")
    result = await check_connection(
        str(provider.base_url),
        provider.model,
        provider.api_key,
        provider.timeout_seconds,
        provider.provider_type,
    )
    return result.model_dump(mode="json", by_alias=True)


# A real chat/completions call (Talk with Lens sending the full tool
# catalog, potentially routed to a reasoning model like DeepSeek's
# deepseek-reasoner) can legitimately take several minutes to produce a
# complete, non-streamed response -- much longer than `provider.timeout_seconds`
# (capped at 120s), which is sized for the lightweight connectivity probes in
# service.py's check_connection, not a full completion. Reusing that field
# here was causing real, slow-but-successful completions to be aborted as a
# 502 ReadTimeout. Use a generous, fixed budget instead, consistent with the
# multi-minute allowances already given to the internal Deployment path and
# MCP tool calls elsewhere in Talk with Lens.
CHAT_COMPLETIONS_TIMEOUT_SECONDS = 600.0


@router.post(
    "/{provider_id}/chat/completions",
    summary="Send a chat completion request through a saved AI provider.",
    description=(
        "Forward an OpenAI-compatible chat completion body to a saved openai-type provider using its server-held "
        "credentials. Defaults to the saved model if omitted and returns the provider JSON response. Other provider "
        "types are rejected."
    ),
    operation_id="proxy_ai_provider_chat_completions",
)
async def proxy_chat_completions(provider_id: str, request: Annotated[dict, Body()]) -> dict:
    """Server-side passthrough for the Playground's "Chat with Lens" External
    AI provider mode: forwards an OpenAI-style ``chat/completions`` body
    (messages/tools/tool_choice) to a saved provider's real endpoint with the
    server-held API key attached, and returns only the provider's JSON
    response. The caller (server/playground/chat.ts) never sees the raw key,
    matching the same secrecy guarantee as ``test_saved_provider`` above.
    """
    provider = _service.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"AI provider not found: {provider_id}")
    if provider.provider_type != "openai":
        raise HTTPException(
            status_code=400,
            detail="Only openai-type AI providers support chat/completions passthrough",
        )
    base_url = str(provider.base_url)
    try:
        await validate_provider_endpoint(base_url)
    except UnsafeProviderEndpointError as error:
        raise HTTPException(
            status_code=422,
            detail="Provider URL must use HTTPS and resolve only to public addresses.",
        ) from error
    endpoint = f"{base_url if base_url.endswith('/') else base_url + '/'}v1/chat/completions"
    headers = {"content-type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    payload = dict(request)
    payload.setdefault("model", provider.model)
    try:
        async with public_provider_client(base_url, CHAT_COMPLETIONS_TIMEOUT_SECONDS) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
    except UnsafeProviderEndpointError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except httpx.HTTPError as error:
        # httpx connection/timeout errors (e.g. ConnectTimeout, ConnectError)
        # often have an empty str(error) -- the useful detail lives on the
        # exception's type name and/or wrapped cause (e.g. the underlying
        # OSError). Without this, the caller only ever saw "AI provider
        # request failed: ", which gives no hint of what actually went wrong.
        logger.warning(
            "event=ai_provider_proxy_failed provider_id=%s error_type=%s", provider_id, type(error).__name__
        )
        raise HTTPException(
            status_code=502,
            detail=f"AI provider request failed ({type(error).__name__}).",
        ) from error
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:1000])
    return response.json()
