"""The only place external tools (kubectl/oc, helm, kustomize, git) are executed.

Arguments are always passed as a list, never through a shell, so the same code
runs on Linux, macOS, and Windows. A runner is injectable so tests can replace
the cluster with an in-memory fake.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .errors import CommandError


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        return (self.stderr or self.stdout).strip()


Runner = Callable[..., Result]


def subprocess_runner(
    argv: Sequence[str],
    *,
    input: str | None = None,
    timeout: float = 300,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    args = [str(item) for item in argv]
    executable = shutil.which(args[0]) or args[0]
    try:
        completed = subprocess.run(
            [executable, *args[1:]],
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            f"{args[0]} is not installed or not on PATH", hint=f"Install {args[0]} and retry."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{display(args)} timed out after {timeout:.0f}s") from exc
    return Result(tuple(args), completed.returncode, completed.stdout, completed.stderr)


def display(argv: Sequence[str]) -> str:
    return shlex.join(str(item) for item in argv)


def checked(result: Result, what: str | None = None) -> str:
    if not result.ok:
        raise CommandError(f"{what or display(result.argv)} failed: {result.message or 'no output'}")
    return result.stdout


def available(tool: str) -> bool:
    return shutil.which(tool) is not None
