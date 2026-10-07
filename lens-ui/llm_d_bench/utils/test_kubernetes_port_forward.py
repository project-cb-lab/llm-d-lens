"""`kubectl port-forward` readiness checks (`_wait_port_forward` et al.).

These guard against a real race: an earlier version treated a port-forward as
"ready" the instant the subprocess was still alive, before kubectl had
actually finished binding/negotiating the tunnel. The first request routed
through it could then hit a "Connection refused" upstream error while a
near-instant retry succeeded. `_wait_port_forward` must only report success
once kubectl has printed its own readiness line *and* the local port truly
accepts a connection -- no client-side retry should be necessary.
"""

from __future__ import annotations

import asyncio

import pytest

from llm_d_bench.utils import kubernetes as k8s


class _FakeProcess:
    """Minimal stand-in for `asyncio.subprocess.Process` used by the readiness helpers."""

    def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode

    def exit(self, code: int = 1) -> None:
        self.returncode = code
        self._exited.set()


def _stream_with(data: bytes = b"", *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    if data:
        reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


async def _free_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


@pytest.mark.asyncio
async def test_ready_once_forwarding_reported_and_port_accepts_connections():
    port = await _free_port()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
    try:
        stdout = _stream_with(f"Forwarding from 127.0.0.1:{port} -> 80\n".encode())
        process = _FakeProcess(stdout, _stream_with())

        ready, message = await k8s._wait_port_forward(process, local_port=port, timeout=2)

        assert ready is True
        assert message == ""
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_not_ready_when_process_exits_before_reporting_forwarding():
    stdout = _stream_with()  # closes immediately, no "Forwarding from" line
    stderr = _stream_with(b"error: unable to forward port because pod is not running\n")
    process = _FakeProcess(stdout, stderr)
    process.exit(1)

    ready, message = await k8s._wait_port_forward(process, local_port=18999, timeout=1)

    assert ready is False
    assert "pod is not running" in message


@pytest.mark.asyncio
async def test_not_ready_when_local_port_never_accepts_a_connection():
    # kubectl claims to be forwarding, but nothing is actually listening on
    # the local port yet -- this is exactly the scenario that used to be
    # misreported as "ready" by the old sleep(1)-then-check-alive logic.
    port = await _free_port()
    stdout = _stream_with(f"Forwarding from 127.0.0.1:{port} -> 80\n".encode())
    process = _FakeProcess(stdout, _stream_with())

    ready, message = await k8s._wait_port_forward(process, local_port=port, timeout=0.5)

    assert ready is False
    assert "never accepted a connection" in message
    # The stuck process must be cleaned up rather than left running.
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_probe_tcp_true_only_when_something_is_listening():
    port = await _free_port()
    assert await k8s._probe_tcp("127.0.0.1", port, timeout=0.5) is False

    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
    try:
        assert await k8s._probe_tcp("127.0.0.1", port, timeout=0.5) is True
    finally:
        server.close()
        await server.wait_closed()
