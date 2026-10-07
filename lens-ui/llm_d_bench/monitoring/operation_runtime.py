"""Shared monitoring operation registration, logging and process transport."""
from __future__ import annotations
import asyncio
import os
import re
from datetime import UTC, datetime

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SENSITIVE_LOG_VALUE = re.compile(r"(?i)(admin\s+password|token|authorization|client[_ -]?secret)(\s*[:=]\s*)(\S+)")


class MonitoringOperationManager:
    def __init__(self, store, log_entry_type):
        self.store = store
        self._log_entry_type = log_entry_type
        self._operations = {}
        self._active = {}
        self._idempotency = {}
        self._lock = asyncio.Lock()
        for operation in self.store.recover_interrupted():
            self._operations[operation.operation_id] = operation

    def get(self, operation_id):
        return self._operations.get(operation_id) or self.store.load(operation_id)

    def active_id(self, context, namespace):
        return self._active.get((context or "", namespace))

    async def queue_operation(self, *, key, idempotency_key, cluster_id, create, run):
        async with self._lock:
            if idempotency_key and idempotency_key in self._idempotency:
                existing = self.get(self._idempotency[idempotency_key])
                if existing:
                    return existing
            if key in self._active:
                raise RuntimeError(self._active[key])
            operation = create()
            self._operations[operation.operation_id] = operation
            self._active[key] = operation.operation_id
            if idempotency_key:
                self._idempotency[idempotency_key] = operation.operation_id
            self.store.save(operation, cluster_id=cluster_id)
            asyncio.create_task(run(operation))
            return operation

    def _append(self, operation, message, level="info"):
        clean = _SENSITIVE_LOG_VALUE.sub(r"\1\2[REDACTED]", _ANSI.sub("", message)).strip()
        if not clean:
            return
        operation.logs.append(self._log_entry_type(
            sequence=operation.logs[-1].sequence + 1 if operation.logs else 1,
            timestamp=datetime.now(UTC), level=level, message=clean,
        ))
        while len(operation.logs) > 2_000 or sum(len(row.message.encode()) for row in operation.logs) > 2 * 1024 * 1024:
            operation.logs.pop(0)


def installation_environment(cluster_id, resolve_kubeconfig):
    allowed = {name: value for name, value in os.environ.items()
               if name in {"PATH", "HOME", "KUBECONFIG", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"}}
    if cluster_id:
        try:
            kubeconfig = resolve_kubeconfig(cluster_id).get("KUBECONFIG")
        except FileNotFoundError:
            kubeconfig = None
        if kubeconfig:
            allowed["KUBECONFIG"] = kubeconfig
    return allowed


async def run_install_command(argv, *, spawn, env, timeout, on_line, cwd=None):
    options = dict(env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    if cwd is not None:
        options["cwd"] = cwd
    process = await spawn(argv, **options)

    async def consume():
        assert process.stdout is not None
        async for raw_line in process.stdout:
            on_line(raw_line.decode(errors="replace"))

    try:
        await asyncio.wait_for(asyncio.gather(process.wait(), consume()), timeout)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return process.returncode
