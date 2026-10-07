"""Versioned input and output contracts for the External AI Providers module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


AIProviderType = Literal["openai", "anthropic"]

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def _api_key_preview(api_key: str) -> str:
    """Return a short, safe-to-display fragment of a secret (never the full value)."""
    if not api_key:
        return ""
    return f"...{api_key[-4:]}" if len(api_key) > 4 else "...."


class AIProvider(StrictModel):
    """A saved, OpenAI-compatible external LLM connection.

    ``api_key`` is persisted (see ``store.py``) but is never returned verbatim
    over the API -- ``api_payload`` projects it as ``hasApiKey``/``apiKeyPreview``
    instead (see ``_api_key_preview``).
    """

    id: str = Field(default_factory=lambda: f"ai-provider-{uuid4().hex[:8]}")
    name: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl = Field(alias="baseUrl")
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", alias="apiKey")
    provider_type: AIProviderType = Field(default="openai", alias="providerType")
    timeout_seconds: float = Field(default=30, gt=0, le=120, alias="timeoutSeconds")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    updated_at: datetime = Field(default_factory=utcnow, alias="updatedAt")

    def api_payload(self) -> dict[str, Any]:
        """Project the provider for HTTP clients, masking the API key."""
        payload = self.model_dump(mode="json", by_alias=True, exclude={"api_key"})
        payload["hasApiKey"] = bool(self.api_key)
        payload["apiKeyPreview"] = _api_key_preview(self.api_key)
        return payload


class AIProviderCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl = Field(alias="baseUrl")
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", alias="apiKey")
    provider_type: AIProviderType = Field(default="openai", alias="providerType")
    timeout_seconds: float = Field(default=30, gt=0, le=120, alias="timeoutSeconds")


class AIProviderUpdateRequest(StrictModel):
    """All fields optional; only supplied fields are changed.

    ``api_key`` of ``None`` (the default, i.e. omitted) leaves the stored key
    untouched -- pass an empty string explicitly to clear it.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: HttpUrl | None = Field(default=None, alias="baseUrl")
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, alias="apiKey")
    provider_type: AIProviderType | None = Field(default=None, alias="providerType")
    timeout_seconds: float | None = Field(default=None, gt=0, le=120, alias="timeoutSeconds")


class AIProviderTestRequest(StrictModel):
    """Test connectivity for an unsaved (draft) configuration."""

    base_url: HttpUrl = Field(alias="baseUrl")
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", alias="apiKey")
    provider_type: AIProviderType = Field(default="openai", alias="providerType")
    timeout_seconds: float = Field(default=30, gt=0, le=120, alias="timeoutSeconds")


class AIProviderTestResult(StrictModel):
    success: bool
    message: str
    model_found: bool | None = Field(default=None, alias="modelFound")
    available_models: list[str] = Field(default_factory=list, alias="availableModels")
    latency_ms: float | None = Field(default=None, alias="latencyMs")
