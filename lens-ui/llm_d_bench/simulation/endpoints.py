"""OpenAI-compatible endpoint inspection."""

from __future__ import annotations

import json
from urllib.parse import urlparse, urlunparse

import aiohttp

from .errors import SimulationConfigurationError

MAX_MODELS_RESPONSE_BYTES = 1024 * 1024


def models_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SimulationConfigurationError("Endpoint URL must be a valid HTTP or HTTPS URL")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/chat/completions"):
        path = path[: -len("/chat/completions")]
    elif not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunparse(parsed._replace(path=f"{path}/models", params="", query="", fragment=""))


async def discover_models(endpoint_url: str) -> dict:
    url = models_url(endpoint_url)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, trust_env=True) as session,
            session.get(
                url,
                allow_redirects=False,
                headers={"Accept": "application/json"},
            ) as response,
        ):
            if response.status < 200 or response.status >= 300:
                raise SimulationConfigurationError(f"Model discovery returned HTTP {response.status}")
            if response.content_length is not None and response.content_length > MAX_MODELS_RESPONSE_BYTES:
                raise SimulationConfigurationError("Model discovery response is too large")
            raw = await response.content.read(MAX_MODELS_RESPONSE_BYTES + 1)
            if len(raw) > MAX_MODELS_RESPONSE_BYTES:
                raise SimulationConfigurationError("Model discovery response is too large")
    except (aiohttp.ClientError, TimeoutError) as error:
        raise SimulationConfigurationError(f"Unable to connect to the endpoint model API: {error}") from error

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SimulationConfigurationError("Model discovery returned invalid JSON") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SimulationConfigurationError("Model discovery response does not contain a data array")
    models = sorted(
        {
            item["id"].strip()
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
        }
    )
    if not models:
        raise SimulationConfigurationError("The endpoint reported no models")
    return {"endpoint": endpoint_url, "models": models}
