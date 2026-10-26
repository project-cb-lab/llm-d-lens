"""The stack's own drain command, run once per role while a roll is in progress.

curvebender-tools stacks guard their serving groups with admission policies: the lws
controller may remove an old group only after a drain tool took it out of the pool and
announced it. The drain tool is the stack's, not llmd's; llmd starts one process per role
before it applies the new template, keeps its output in a log, and stops it on pause.

The command is a template. ``{role}``, ``{source}`` (the checkout's root), ``{namespace}``,
``{set}``, ``{old_revision}`` and ``{max_unavailable}`` (the role's groups per step) are
filled in; it runs in the source checkout with the user's kube context in its environment.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import UsageError
from ..yamlio import read_json, write_json_atomic

FIELDS = ("role", "source", "namespace", "set", "old_revision", "max_unavailable")


@dataclass
class Drainer:
    role: str
    argv: list[str]
    log: Path
    pid: int = 0

    def describe(self) -> dict[str, Any]:
        return {"role": self.role, "command": shlex.join(self.argv), "log": str(self.log), "pid": self.pid}


def command(template: str, values: dict[str, str]) -> list[str]:
    """The drain command for one role, split like a shell would, with the fields filled in."""
    try:
        argv = shlex.split(template)
    except ValueError as exc:
        raise UsageError(f"--drain is not a valid command line: {exc}") from exc
    if not argv:
        raise UsageError("--drain is empty")
    try:
        return [part.format(**values) for part in argv]
    except (KeyError, IndexError) as exc:
        raise UsageError(f"--drain names an unknown field {exc}; fields: {', '.join(FIELDS)}") from exc


class Drainers:
    """The running drainers of one instance's rollout, recorded in ``<directory>/drainers.json``."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "drainers.json"

    def load(self) -> list[Drainer]:
        return [Drainer(**item) for item in read_json(self.path, default=[]) or []]

    def save(self, drainers: list[Drainer]) -> None:
        write_json_atomic(self.path, [{**vars(item), "log": str(item.log)} for item in drainers])

    def start(
        self, drainers: list[Drainer], *, cwd: Path, env: dict[str, str] | None = None
    ) -> list[Drainer]:
        self.directory.mkdir(parents=True, exist_ok=True)
        started = []
        for drainer in drainers:
            log = open(drainer.log, "a", encoding="utf-8")  # noqa: SIM115 - the child keeps it open
            process = subprocess.Popen(  # noqa: S603 - the user's own --drain command, never a shell
                drainer.argv,
                cwd=str(cwd),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env={**os.environ, **(env or {})},
                start_new_session=True,
            )
            log.close()
            started.append(Drainer(drainer.role, drainer.argv, drainer.log, process.pid))
        self.save(started)
        return started

    def running(self) -> list[Drainer]:
        return [drainer for drainer in self.load() if drainer.pid and alive(drainer.pid)]

    def stop(self) -> list[Drainer]:
        stopped = []
        for drainer in self.load():
            if drainer.pid and alive(drainer.pid):
                try:
                    os.killpg(drainer.pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                stopped.append(drainer)
        self.save([Drainer(item.role, item.argv, item.log, 0) for item in self.load()])
        return stopped

    def exited(self) -> list[Drainer]:
        """Drainers that were started and are no longer running (finished, or failed)."""
        return [drainer for drainer in self.load() if drainer.pid and not alive(drainer.pid)]


def alive(pid: int) -> bool:
    try:
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if finished:
            return False
    except ChildProcessError:
        pass  # not our child (another llmd process started it): ask the kernel
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tail(path: Path, lines: int = 3) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []
