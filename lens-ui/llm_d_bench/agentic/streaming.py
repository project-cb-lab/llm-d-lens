"""Domain-local SSE delivery for Agentic planning and refinement."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi.responses import StreamingResponse

from .models import AgenticDeploymentRun

Progress = Callable[[dict[str, object]], Awaitable[None]]


def planning_stream_response(
    run: Callable[[Progress], Awaitable[AgenticDeploymentRun]],
    *,
    expected_errors: tuple[type[Exception], ...],
    failure_message: str,
    on_unexpected_error: Callable[[], None] | None = None,
) -> StreamingResponse:
    async def events():
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def on_progress(event: dict[str, object]) -> None:
            await queue.put({"event": "progress", "data": event})

        async def produce() -> None:
            try:
                result = await run(on_progress)
                await queue.put({"event": "complete", "data": result.model_dump(mode="json")})
            except expected_errors as error:
                await queue.put({"event": "error", "data": {"message": str(error)}})
            except Exception:
                if on_unexpected_error is not None:
                    on_unexpected_error()
                await queue.put({"event": "error", "data": {"message": failure_message}})

        task = asyncio.create_task(produce())
        try:
            while True:
                item = await queue.get()
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item["event"] in {"complete", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
