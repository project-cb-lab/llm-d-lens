"""Request-count progress shared by local and in-cluster Simulation execution."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .process import RunContext


async def report_request_progress(
    context: RunContext,
    request_counts: Callable[[], tuple[int, int | None] | None],
    *,
    started_monotonic: float | None = None,
    ready_event: asyncio.Event | None = None,
) -> None:
    if ready_event is not None:
        await ready_event.wait()
    if started_monotonic is None:
        started_monotonic = asyncio.get_running_loop().time()
    while True:
        counts = await asyncio.to_thread(request_counts)
        if counts is not None:
            completed, expected = counts
            elapsed = asyncio.get_running_loop().time() - started_monotonic
            percent = min(99, completed / expected * 100) if expected is not None and expected > 0 else 0
            request_progress = (
                f"{completed} / {expected} requests completed"
                if expected is not None else f"{completed} requests completed"
            )
            context.progress(percent, f"Simulation running · {elapsed:.0f}s elapsed · {request_progress}")
        await asyncio.sleep(1)
