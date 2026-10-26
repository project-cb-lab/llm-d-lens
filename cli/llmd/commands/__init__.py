"""Command-line commands. Each module adds its subcommands with ``register(subparsers)`` and
returns ``{name: handler}``; a handler takes a :class:`Context` and returns the exit code."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths, yamlio
from ..process import Runner, subprocess_runner
from ..sources import Source, SourceRegistry, resolve_source

#: Tests set this to an in-memory cluster; it is never set in normal use.
RUNNER: Runner | None = None


@dataclass
class Context:
    args: argparse.Namespace

    @property
    def runner(self) -> Runner:
        return RUNNER or subprocess_runner

    @property
    def state_dir(self) -> Path:
        return paths.state_dir(self.args.state_dir or None)

    @property
    def registry(self) -> SourceRegistry:
        return SourceRegistry(self.state_dir)

    def source(self) -> Source:
        """The ``--source`` of the command; with exactly one registered source it may be left out."""
        spec = getattr(self.args, "source", "") or ""
        if not spec:
            entries = self.registry.entries()
            if len(entries) == 1:
                spec = entries[0]["name"]
        return resolve_source(
            spec, registry=self.registry, runner=self.runner, refresh=getattr(self.args, "refresh", False)
        )

    def emit(self, value: Any) -> None:
        emit_value(value, self.args.output)


Handler = Callable[[Context], int]


def emit_value(value: Any, output: str) -> None:
    if output == "json":
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        print(yamlio.dump(value), end="")


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        default="",
        metavar="NAME|PATH|URL#REF",
        help="where the stack comes from: a registered name, a local checkout, or a Git URL with a ref "
        "(optional when exactly one source is registered)",
    )
    parser.add_argument("--refresh", action="store_true", help="fetch a remote source again")
