"""Explicit, event-loop-local Kubernetes HTTP client lifecycle and bounded reuse.

Configurations must be freshly loaded/validated by the caller before each use.
Only transports are pooled; responses, cluster resolution and credentials are
never reused in place of that validation. This module owns configuration cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from kubernetes.aio import client

from llm_d_bench.utils.kubernetes_auth import cleanup_configuration

_MAX_CLIENTS = 16
_MAX_CONCURRENCY = 16
_IDLE_SECONDS = 30.0
_POOLS = WeakKeyDictionary()


def _fingerprint(configuration) -> str:
    # Temporary credential file names differ per fresh exec authentication. Hash
    # their contents rather than names, and retain the original files until close.
    fields = (
        "host",
        "verify_ssl",
        "disable_strict_ssl_verification",
        "assert_hostname",
        "tls_server_name",
        "api_key",
        "api_key_prefix",
        "username",
        "password",
        "proxy",
        "proxy_headers",
        "connection_pool_maxsize",
        "retries",
        "socket_options",
        "safe_chars_for_path_param",
        "client_side_validation",
        "discard_unknown_keys",
        "server_index",
        "server_variables",
        "server_operation_index",
        "server_operation_variables",
    )
    values = {field: getattr(configuration, field, None) for field in fields}
    for field in ("ssl_ca_cert", "cert_file", "key_file"):
        filename = getattr(configuration, field, None)
        values[field] = hashlib.sha256(Path(filename).read_bytes()).hexdigest() if filename else None
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=repr).encode()).hexdigest()


@dataclass(eq=False)
class _Entry:
    key: str
    api: object
    configuration: object
    users: int = 0
    idle_since: float = 0.0

    async def close(self):
        try:
            await self.api.close()
        finally:
            cleanup_configuration(self.configuration)


class _Pool:
    def __init__(self):
        self.entries: list[_Entry] = []
        self.condition = asyncio.Condition()
        self.capacity = asyncio.Semaphore(_MAX_CONCURRENCY)
        self.closing = False
        self.close_task = None

    async def acquire(self, configuration):
        owned = True
        acquired = False
        try:
            key = _fingerprint(configuration)
            await self.capacity.acquire()
            acquired = True
            async with self.condition:
                while True:
                    if self.closing:
                        raise RuntimeError("Kubernetes client pool is closing")
                    now = time.monotonic()
                    for entry in list(self.entries):
                        if not entry.users and now - entry.idle_since >= _IDLE_SECONDS:
                            self.entries.remove(entry)
                            await _finish_cleanup(entry.close())
                    for entry in self.entries:
                        if entry.key != key:
                            continue
                        # A refresh hook or rotated file can mutate a retained
                        # configuration. Its original key no longer proves that
                        # this transport was created with the fresh credentials.
                        try:
                            matches = _fingerprint(entry.configuration) == key
                        except OSError:
                            matches = False
                        if matches:
                            cleanup_configuration(configuration)
                            owned = False
                            entry.users += 1
                            return entry
                    if len(self.entries) >= _MAX_CLIENTS:
                        idle = [entry for entry in self.entries if not entry.users]
                        if not idle:
                            await self.condition.wait()
                            continue
                        oldest = min(idle, key=lambda entry: entry.idle_since)
                        self.entries.remove(oldest)
                        await _finish_cleanup(oldest.close())
                    api = client.ApiClient(configuration=configuration)
                    entry = _Entry(key, api, configuration, users=1)
                    self.entries.append(entry)
                    owned = False
                    return entry
        except BaseException:
            if acquired:
                self.capacity.release()
            if owned:
                cleanup_configuration(configuration)
            raise

    async def release(self, entry):
        async with self.condition:
            entry.users -= 1
            if not entry.users:
                entry.idle_since = time.monotonic()
            self.capacity.release()
            self.condition.notify_all()

    async def close(self):
        async with self.condition:
            self.closing = True
            self.condition.notify_all()
            await self.condition.wait_for(lambda: all(not entry.users for entry in self.entries))
            entries, self.entries = self.entries, []
        results = await asyncio.gather(*(entry.close() for entry in entries), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def start_pool() -> None:
    """Enable pooling for this event loop's application lifetime (idempotent)."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop)
    if pool is not None and pool.closing:
        raise RuntimeError("Kubernetes client pool is closing")
    if pool is None:
        _POOLS[loop] = _Pool()


async def _finish_cleanup(awaitable):
    # Cancellation of a request/shutdown must not abandon an owned HTTP session
    # or leave an active reference preventing application shutdown forever.
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def close_pool() -> None:
    """Wait for borrowers, close all clients and remove this loop's pool."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop)
    if pool is None:
        return
    if pool.close_task is None:
        pool.closing = True
        pool.close_task = asyncio.create_task(pool.close())
    try:
        await _finish_cleanup(pool.close_task)
    finally:
        if _POOLS.get(loop) is pool:
            del _POOLS[loop]


@asynccontextmanager
async def pooled_client(configuration):
    """Borrow a client; consume configuration ownership even on failure/cancel."""
    pool = _POOLS.get(asyncio.get_running_loop())
    if pool is None:
        api = None
        try:
            api = client.ApiClient(configuration=configuration)
            yield api
        finally:
            if api is None:
                cleanup_configuration(configuration)
            else:
                await _finish_cleanup(_Entry("", api, configuration).close())
        return
    entry = await pool.acquire(configuration)
    try:
        yield entry.api
    finally:
        await _finish_cleanup(pool.release(entry))
