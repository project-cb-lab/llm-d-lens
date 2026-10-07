"""Thin, read-only client for the public HuggingFace Hub API.

Used only to power the model source picker in the Download Model modal
(search models, show README + tags for a selected repo). The actual model
download still happens via ``hf download`` (the huggingface_hub CLI) in Deploy/model-cache
provisioning jobs -- this module never writes anything and never requires a
token, since it only reads public listing/metadata endpoints.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

HF_API_BASE = "https://huggingface.co"
_TIMEOUT = httpx.Timeout(8.0)


class HuggingFaceHubError(RuntimeError):
    """Raised when the HuggingFace Hub API is unreachable or returns an error."""


class HuggingFaceModelNotFoundError(HuggingFaceHubError):
    """Raised when a specific repo id does not exist on the Hub."""


_REPO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


async def search_models(query: str, limit: int = 20) -> list[dict]:
    """Search public models on the Hub, ranked by the Hub's own relevance/sort."""
    params = {"search": query, "limit": str(max(1, min(limit, 50))), "full": "false"}
    async with httpx.AsyncClient(base_url=HF_API_BASE, timeout=_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get("/api/models", params=params)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HuggingFaceHubError(f"HuggingFace Hub search failed: {error}") from error
    payload = response.json()
    return payload if isinstance(payload, list) else []


async def get_model_detail(repo_id: str) -> dict:
    """Fetch model metadata plus the README (if any) for a single repo id."""
    if not _REPO_ID_PATTERN.fullmatch(repo_id) or ".." in repo_id:
        raise HuggingFaceModelNotFoundError(f"model {repo_id!r} was not found on the HuggingFace Hub")
    encoded_repo_id = "/".join(quote(part, safe="") for part in repo_id.split("/"))
    async with httpx.AsyncClient(base_url=HF_API_BASE, timeout=_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(f"/api/models/{encoded_repo_id}")
        except httpx.HTTPError as error:
            raise HuggingFaceHubError(f"HuggingFace Hub lookup failed: {error}") from error
        if response.status_code in (404, 401):
            # The public Hub API returns 401 (not 404) for unauthenticated
            # lookups of nonexistent repos, presumably to avoid leaking the
            # existence of private repos. Since this client never
            # authenticates, both codes mean "not found" for our purposes.
            raise HuggingFaceModelNotFoundError(f"model {repo_id!r} was not found on the HuggingFace Hub")
        try:
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HuggingFaceHubError(f"HuggingFace Hub lookup failed: {error}") from error
        info = response.json()

        readme = ""
        try:
            readme_response = await client.get(f"/{encoded_repo_id}/raw/main/README.md")
            if readme_response.status_code == 200:
                readme = readme_response.text
        except httpx.HTTPError:
            # README is a nice-to-have; a fetch failure shouldn't fail the lookup.
            readme = ""
    return {"info": info if isinstance(info, dict) else {}, "readme": readme}
