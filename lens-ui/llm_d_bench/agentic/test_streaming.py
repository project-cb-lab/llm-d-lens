"""Route-level contracts for shared planning SSE delivery."""
import asyncio
from types import SimpleNamespace

import pytest

from . import router


async def response_for(monkeypatch, mode, work):
    monkeypatch.setattr(router, "service", SimpleNamespace(create=work, refine=work))
    if mode == "create":
        return await router.stream_agentic_deployment(SimpleNamespace(), None)
    return await router.stream_refine_agentic_deployment("run-1", SimpleNamespace(), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "refine"])
async def test_progress_precedes_one_complete_event(monkeypatch, mode):
    async def work(*args, on_progress, principal_id):
        assert principal_id is None
        await on_progress({"phase": "planning"})
        return SimpleNamespace(model_dump=lambda **kwargs: {"id": "run-1"})

    response = await response_for(monkeypatch, mode, work)
    assert response.media_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert [chunk async for chunk in response.body_iterator] == [
        'event: progress\ndata: {"phase": "planning"}\n\n',
        'event: complete\ndata: {"id": "run-1"}\n\n',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,error,expected", [
    ("create", ValueError("invalid"), "invalid"),
    ("refine", ValueError("invalid"), "invalid"),
    ("refine", KeyError("missing"), "'missing'"),
    ("create", KeyError("missing"), "Unable to generate an Agentic recommendation."),
    ("refine", RuntimeError("internal detail"), "Unable to recalculate the Agentic recommendation."),
])
async def test_error_contracts_remain_route_specific(monkeypatch, mode, error, expected):
    async def work(*args, **kwargs):
        raise error

    response = await response_for(monkeypatch, mode, work)
    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 1
    assert chunks[0].startswith("event: error\n")
    assert expected in chunks[0]
    assert "internal detail" not in chunks[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "refine"])
async def test_closing_stream_cancels_and_awaits_planning_work(monkeypatch, mode):
    stopped = asyncio.Event()

    async def work(*args, on_progress, **kwargs):
        try:
            await on_progress({"phase": "planning"})
            await asyncio.Event().wait()
        finally:
            stopped.set()

    response = await response_for(monkeypatch, mode, work)
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert stopped.is_set()
