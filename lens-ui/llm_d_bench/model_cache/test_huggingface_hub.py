"""Tests for the HuggingFace Hub client (llm_d_bench.model_cache.huggingface_hub).

httpx.AsyncClient is monkeypatched with a small in-memory fake so these tests
never hit the network (no respx/httpx-mock dependency in this project).
"""

import httpx
import pytest

from llm_d_bench.model_cache import huggingface_hub


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://huggingface.co/fake")
            raise httpx.HTTPStatusError(
                "error", request=request, response=httpx.Response(self.status_code, request=request)
            )

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path, params=None):
        self.calls.append((path, params))
        return self._responses.pop(0)


def _install_fake_client(monkeypatch, responses):
    fake = _FakeClient(responses)

    def factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(huggingface_hub.httpx, "AsyncClient", factory)
    return fake


@pytest.mark.asyncio
async def test_search_models_returns_list(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(200, json_data=[{"id": "org/model", "likes": 3}])])
    items = await huggingface_hub.search_models("llama")
    assert items == [{"id": "org/model", "likes": 3}]


@pytest.mark.asyncio
async def test_search_models_raises_on_http_error(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(500)])
    with pytest.raises(huggingface_hub.HuggingFaceHubError):
        await huggingface_hub.search_models("llama")


@pytest.mark.asyncio
async def test_get_model_detail_includes_readme(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(200, json_data={"id": "org/model", "tags": ["text-generation"]}),
            _FakeResponse(200, text="# Hello"),
        ],
    )
    detail = await huggingface_hub.get_model_detail("org/model")
    assert detail["info"]["id"] == "org/model"
    assert detail["readme"] == "# Hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_id", ["org/../../admin", "org/model?next=evil", "https://example.com/model", "model"])
async def test_get_model_detail_rejects_invalid_repo_ids_before_request(monkeypatch, repo_id):
    client = _install_fake_client(monkeypatch, [])

    with pytest.raises(huggingface_hub.HuggingFaceModelNotFoundError):
        await huggingface_hub.get_model_detail(repo_id)

    assert client.calls == []


@pytest.mark.asyncio
async def test_get_model_detail_tolerates_missing_readme(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(200, json_data={"id": "org/model"}),
            _FakeResponse(404),
        ],
    )
    detail = await huggingface_hub.get_model_detail("org/model")
    assert detail["readme"] == ""


@pytest.mark.asyncio
async def test_get_model_detail_raises_not_found(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(404)])
    with pytest.raises(huggingface_hub.HuggingFaceModelNotFoundError):
        await huggingface_hub.get_model_detail("org/missing")


@pytest.mark.asyncio
async def test_get_model_detail_raises_not_found_on_401(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(401)])
    with pytest.raises(huggingface_hub.HuggingFaceModelNotFoundError):
        await huggingface_hub.get_model_detail("org/missing")


@pytest.mark.asyncio
async def test_get_model_detail_raises_on_upstream_error(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(500)])
    with pytest.raises(huggingface_hub.HuggingFaceHubError):
        await huggingface_hub.get_model_detail("org/model")
