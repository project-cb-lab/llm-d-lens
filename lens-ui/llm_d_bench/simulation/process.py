"""Safe subprocess execution for simulation backends."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from llm_d_bench.utils.shell import CommandNotFoundError, run_sync, spawn

from .errors import SimulationCancelledError, SimulationExecutionError
from .models import BackendAvailability
from .progress import report_request_progress


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CommandResult:
    return_code: int
    started_at: datetime
    completed_at: datetime
    stdout_path: Path
    stderr_path: Path
    timed_out: bool = False


@dataclass
class RunContext:
    cancel_event: asyncio.Event
    log: Callable[[str], None]
    progress: Callable[[float, str], None]
    process: asyncio.subprocess.Process | None = None
    # Called (at most once) the moment the backend tool is confirmed staged/
    # installed and the real benchmark command starts running, so callers can
    # anchor an elapsed-time clock to "tool ready" rather than "task picked
    # up" (which also covers pod launch/data staging/on-demand tool install).
    on_ready: Callable[[], None] | None = None
    artifacts_incomplete: bool = False


def executable_availability(executable: str) -> BackendAvailability:
    try:
        probe = run_sync([executable, "--version"], timeout=10)
    except CommandNotFoundError:
        return BackendAvailability(
            available=False,
            version=None,
            unavailable_reason=f"Executable '{executable}' was not found in PATH",
        )
    except (OSError, TimeoutError):
        return BackendAvailability(available=True, version=None, unavailable_reason=None)
    output = (probe.stdout or probe.stderr or "").strip().splitlines()
    version = output[0][:200] if probe.returncode == 0 and output else None
    return BackendAvailability(available=True, version=version, unavailable_reason=None)


async def terminate_process(process: asyncio.subprocess.Process, grace_seconds: float = 5) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), grace_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _copy_stream(
    stream: asyncio.StreamReader,
    destination,
    log: Callable[[str], None],
) -> None:
    while chunk := await stream.read(64 * 1024):
        destination.write(chunk)
        destination.flush()
        message = chunk.decode("utf-8", errors="replace").rstrip()
        if message:
            log(message)


async def execute_command(
    executable: str,
    args: Sequence[str],
    artifact_dir: Path,
    timeout_seconds: int,
    context: RunContext,
    progress_request_counts: Callable[[], tuple[int, int | None] | None] | None = None,
    tolerate_timeout: bool = False,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "stdout.log"
    stderr_path = artifact_dir / "stderr.log"
    started_at = utc_now()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = await spawn(
                [executable, *args],
                env={**os.environ, **env} if env else None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SimulationExecutionError(str(error)) from error
        context.process = process
        # Host-mode execution has no in-process install/staging step (the
        # backend's ``prepare_executable`` already finished before this is
        # called), so the tool is ready the instant the process is spawned.
        if context.on_ready is not None:
            context.on_ready()
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(_copy_stream(process.stdout, stdout, context.log))
        stderr_task = asyncio.create_task(_copy_stream(process.stderr, stderr, context.log))
        exit_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(context.cancel_event.wait())
        timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
        progress_task: asyncio.Task | None = None
        if progress_request_counts is not None:
            started_monotonic = asyncio.get_running_loop().time()

            progress_task = asyncio.create_task(report_request_progress(
                context, progress_request_counts, started_monotonic=started_monotonic,
            ))

        try:
            done, _ = await asyncio.wait(
                {exit_task, cancel_task, timeout_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                await terminate_process(process)
                await asyncio.gather(stdout_task, stderr_task)
                raise SimulationCancelledError("Simulation was cancelled")
            if timeout_task in done:
                context.artifacts_incomplete = True
                await terminate_process(process)
                await asyncio.gather(stdout_task, stderr_task)
                if tolerate_timeout:
                    return CommandResult(
                        return_code=process.returncode if process.returncode is not None else -1,
                        started_at=started_at,
                        completed_at=utc_now(),
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        timed_out=True,
                    )
                raise SimulationExecutionError(f"Simulation timed out after {timeout_seconds} seconds")
            return_code = exit_task.result()
            await asyncio.gather(stdout_task, stderr_task)
            if return_code != 0:
                tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise SimulationExecutionError(f"Simulation exited with code {return_code}: {tail}")
            return CommandResult(
                return_code=return_code,
                started_at=started_at,
                completed_at=utc_now(),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except asyncio.CancelledError:
            await terminate_process(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            for task in (exit_task, cancel_task, timeout_task):
                if not task.done():
                    task.cancel()
            if progress_task is not None:
                progress_task.cancel()
            await asyncio.gather(
                exit_task,
                cancel_task,
                timeout_task,
                *((progress_task,) if progress_task is not None else ()),
                return_exceptions=True,
            )
            context.process = None
