"""Unified shell/process execution client for all ``llm_d_bench`` workflows.

Every command invocation in ``llm_d_bench`` (``subprocess.run``,
``asyncio.create_subprocess_exec``, ``subprocess.Popen``, and ``/bin/sh -lc``
scripts) must go through this module so that executable discovery, argument
handling, timeout enforcement, and output decoding behave consistently.

Typical usage::

    from llm_d_bench.utils.shell import CommandNotFoundError, CommandResult, shell

    result = await shell.run(["kubectl", "get", "pods"])          # async capture
    result = shell.run_sync(["git", "rev-parse", "HEAD"])         # sync capture
    result = await shell.run_script("ls -la /tmp")                # /bin/sh -lc
    process = await shell.spawn(["kubectl", "port-forward", ...]) # long-running (async)
    process = shell.popen([...])                                  # long-running (sync)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


def kubectl_cache_dir(kubeconfig: str | None) -> Path:
    """Resolve the isolated ``--cache-dir`` used for a given ``KUBECONFIG``.

    Shared with :func:`_with_kubectl_cache_dir` so callers that need to
    invalidate the cache (e.g. right after installing new CRDs) compute the
    exact same path.
    """
    resolved = kubeconfig or str(Path.home() / ".kube" / "config")
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
    return Path.home() / ".llm-d-lens" / "kube-cache" / digest


def invalidate_kubectl_discovery_cache(kubeconfig: str | None) -> None:
    """Drop the cached API-group/REST-mapping discovery for one cluster.

    ``kubectl``'s on-disk discovery cache has a long TTL (hours), so once it
    has recorded that a CRD's API group doesn't exist (e.g. queried before
    Envoy Gateway/Agent Router CRDs were installed), it keeps serving that
    stale "unknown resource" answer well after the CRD becomes ``Established``
    -- causing ``apply`` to fail with "no matches for kind ... ensure CRDs are
    installed first" even though the CRD is genuinely ready. Deleting only
    ``discovery/`` is not enough: the separate ``http/`` response cache stores
    the raw ``/apis`` bodies keyed by ETag, and the server happily answers the
    conditional re-fetch with "304 Not Modified" against that stale ETag,
    silently reconstructing the exact same stale discovery data. Both
    subdirectories must go so the next ``kubectl`` invocation is forced to
    fetch discovery from scratch.
    """
    cache_dir = kubectl_cache_dir(kubeconfig)
    for subdir in ("discovery", "http"):
        path = cache_dir / subdir
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def _with_kubectl_cache_dir(argv: Sequence[str], env: Mapping[str, str] | None) -> list[str]:
    """Insert an isolated ``--cache-dir`` for ``kubectl`` invocations.

    ``kubectl``'s on-disk discovery/REST-mapping cache defaults to
    ``~/.kube/cache``, keyed only by API server host:port -- shared by every
    cluster this process ever talks to (and, on a multi-tenant host, by every
    other user's/tool's ``kubectl`` too). If two different clusters' API
    servers ever reuse the same host:port (a reassigned/rebuilt cluster behind
    the same proxy/tunnel address is common here), one cluster's stale
    discovery data gets served for another, producing intermittent
    ``couldn't get current server API group list`` / ``Error from server
    (NotFound): the server could not find the requested resource`` failures
    that have nothing to do with that cluster's actual health. Keying the
    cache directory off the effective ``KUBECONFIG`` instead (and keeping it
    under ``~/.llm-d-lens`` rather than ``~/.kube``, per this app's rule that
    all of its unstructured data lives there) isolates caches per cluster
    while still caching normally across repeated calls to the same one.
    """
    if not argv or Path(str(argv[0])).name != "kubectl":
        return list(argv)
    if any(str(arg) == "--cache-dir" or str(arg).startswith("--cache-dir=") for arg in argv):
        return list(argv)
    kubeconfig = (env or {}).get("KUBECONFIG") or os.environ.get("KUBECONFIG")
    cache_dir = kubectl_cache_dir(kubeconfig)
    return [argv[0], f"--cache-dir={cache_dir}", *argv[1:]]


@dataclass(frozen=True)
class CommandResult:
    """Decoded result of a completed command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandNotFoundError(FileNotFoundError):
    """Raised when a required executable cannot be resolved on PATH."""


class CommandTimeoutError(TimeoutError):
    """Raised when a command exceeds its configured timeout."""


def which(executable: str) -> str | None:
    """Resolve an executable on PATH (thin wrapper over :func:`shutil.which`)."""
    return shutil.which(executable)


def resolve_executable(argv: Sequence[str]) -> str:
    """Resolve ``argv[0]`` to an executable path, raising on failure.

    Absolute paths are used as-is when the file exists; otherwise the command is
    looked up on PATH. Raises :class:`CommandNotFoundError` when it cannot be
    resolved.
    """
    if not argv:
        raise ValueError("Command cannot be empty")
    command = str(argv[0])
    if os.path.isabs(command):
        if os.path.isfile(command):
            return command
        raise CommandNotFoundError(f"{command} is not installed")
    resolved = shutil.which(command)
    if resolved is None:
        raise CommandNotFoundError(f"{command} is not installed")
    return resolved


def _display(argv: Sequence[str]) -> str:
    return " ".join(str(argument) for argument in argv)


class ShellClient:
    """Executes commands with consistent discovery, timeout, and decoding."""

    def which(self, executable: str) -> str | None:
        return shutil.which(executable)

    def executable(self, executable: str) -> str | None:
        """Alias for :meth:`which`, kept for compatibility with ``CommandRunner``."""
        return self.which(executable)

    def resolve(self, argv: Sequence[str]) -> str:
        return resolve_executable(argv)

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | object | None = None,
        stdout: int | object | None = None,
        stderr: int | object | None = None,
        start_new_session: bool = False,
        executable: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Launch a command without waiting for completion (async)."""
        argv = _with_kubectl_cache_dir(argv, env)
        resolved = executable if executable is not None else self.resolve(argv)
        return await asyncio.create_subprocess_exec(
            resolved,
            *[str(argument) for argument in argv[1:]],
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )

    async def spawn_with_executable(
        self,
        executable: str | Path,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | object | None = None,
        stdout: int | object | None = None,
        stderr: int | object | None = None,
        start_new_session: bool = False,
    ) -> asyncio.subprocess.Process:
        """Launch a fixed executable with arguments kept separate from it."""
        executable_path = str(executable)
        resolved = self.resolve([executable_path])
        argv = _with_kubectl_cache_dir([executable_path, *args], env)
        return await asyncio.create_subprocess_exec(
            resolved,
            *[str(argument) for argument in argv[1:]],
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,  # noqa: A002 - subprocess-compatible public keyword
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command to completion, capturing decoded stdout/stderr (async)."""
        process = await self.spawn(
            argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        data = input.encode() if isinstance(input, str) else input
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise CommandTimeoutError(f"Command timed out after {timeout}s: {_display(argv)}") from error
        return CommandResult(
            argv=tuple(str(argument) for argument in argv),
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    def run_sync(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,  # noqa: A002 - subprocess-compatible public keyword
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command to completion, capturing decoded stdout/stderr (sync)."""
        argv = _with_kubectl_cache_dir(argv, env)
        data = input.encode() if isinstance(input, str) else input
        arguments = [str(argument) for argument in argv]
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                env=env,
                input=data,
                stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise CommandNotFoundError(str(error)) from error
        except subprocess.TimeoutExpired as error:
            raise CommandTimeoutError(f"Command timed out after {timeout}s: {_display(argv)}") from error
        return CommandResult(
            argv=tuple(str(argument) for argument in argv),
            returncode=completed.returncode or 0,
            stdout=completed.stdout.decode(errors="replace"),
            stderr=completed.stderr.decode(errors="replace"),
        )

    async def run_script(
        self,
        script: str,
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a shell script via ``/bin/sh -lc`` and capture its output."""
        return await self.run(["/bin/sh", "-lc", script], cwd=cwd, env=env, timeout=timeout)

    def popen(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | object | None = None,
        stdout: int | object | None = None,
        stderr: int | object | None = None,
        **kwargs: object,
    ) -> subprocess.Popen:
        """Launch a long-running command without waiting (sync ``Popen``)."""
        argv = _with_kubectl_cache_dir(argv, env)
        executable = self.resolve(argv)
        return subprocess.Popen(
            [executable, *[str(argument) for argument in argv[1:]]],
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **kwargs,
        )


#: Shared singleton used by the module-level helpers below.
shell = ShellClient()


class CommandRunner(ShellClient):
    """Runner that applies a default 10-second timeout to every ``run`` call.

    Kept as a distinct class (rather than a plain alias) so callers get a bounded
    execution time without having to pass an explicit ``timeout`` on each call.
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,  # noqa: A002 - subprocess-compatible public keyword
        timeout: float | None = 10,
    ) -> CommandResult:
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


class ScopedCommandRunner(CommandRunner):
    """Runner that overlays a fixed environment onto every spawned command.

    Used to point ``kubectl``/``helm`` at a specific cluster's kubeconfig without
    threading an ``env`` argument through every discovery call. Any environment
    explicitly passed to ``run``/``spawn`` takes precedence over the overlay.
    """

    def __init__(self, env: Mapping[str, str]) -> None:
        super().__init__()
        self._env = {str(key): str(value) for key, value in env.items()}

    def _merged_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        return {**self._env, **{str(key): str(value) for key, value in (env or {}).items()}}

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,  # noqa: A002 - subprocess-compatible public keyword
        timeout: float | None = 10,
    ) -> CommandResult:
        return await super().run(argv, cwd=cwd, env=self._merged_env(env), input=input, timeout=timeout)

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | object | None = None,
        stdout: int | object | None = None,
        stderr: int | object | None = None,
        start_new_session: bool = False,
        executable: str | None = None,
    ) -> asyncio.subprocess.Process:
        return await super().spawn(
            argv,
            cwd=cwd,
            env=self._merged_env(env),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
            executable=executable,
        )


async def run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,  # noqa: A002 - subprocess-compatible public keyword
    timeout: float | None = None,
) -> CommandResult:
    """Async capture via the shared :data:`shell` client."""
    return await shell.run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


def run_sync(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,  # noqa: A002 - subprocess-compatible public keyword
    timeout: float | None = None,
) -> CommandResult:
    """Sync capture via the shared :data:`shell` client."""
    return shell.run_sync(argv, cwd=cwd, env=env, input=input, timeout=timeout)


async def run_script(
    script: str,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Shell-script capture via the shared :data:`shell` client."""
    return await shell.run_script(script, cwd=cwd, env=env, timeout=timeout)


async def spawn(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | object | None = None,
    stdout: int | object | None = None,
    stderr: int | object | None = None,
    start_new_session: bool = False,
    executable: str | None = None,
) -> asyncio.subprocess.Process:
    """Async spawn via the shared :data:`shell` client."""
    return await shell.spawn(
        argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
        executable=executable,
    )


def popen(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | object | None = None,
    stdout: int | object | None = None,
    stderr: int | object | None = None,
    **kwargs: object,
) -> subprocess.Popen:
    """Sync spawn via the shared :data:`shell` client."""
    return shell.popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        **kwargs,
    )
