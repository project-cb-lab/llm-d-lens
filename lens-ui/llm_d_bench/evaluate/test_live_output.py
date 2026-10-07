"""Exercise real subprocess pipes: output must be visible before exit."""

import asyncio
import importlib
import sys

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


@pytest.mark.asyncio
async def test_output_is_published_before_exit(monkeypatch):
    saved = []
    monkeypatch.setattr(router, "_save", lambda run: saved.append(dict(run)))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        'import time,sys; print("working"); print("diagnostic",file=sys.stderr); time.sleep(3)',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    run = {"wait_timeout_seconds": 10}
    out, err = await router._stream_benchmark_process(process, run)
    assert b"working" in out and b"diagnostic" in err
    assert any(item.get("stdout") and item.get("heartbeat_at") for item in saved[:-1])
    assert run["last_log_at"]


@pytest.mark.asyncio
async def test_silent_process_times_out(monkeypatch):
    monkeypatch.setattr(router, "_save", lambda run: None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    with pytest.raises(RuntimeError, match="execution timeout"):
        await router._stream_benchmark_process(process, {"wait_timeout_seconds": 0.1})
    assert process.returncode is not None
