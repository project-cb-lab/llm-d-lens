"""Command-line commands. Each module adds its subcommands with ``register(subparsers)`` and
returns ``{name: handler}``; a handler takes a :class:`Context` and returns the exit code."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths, yamlio
from ..kube import Kube, Target
from ..model import validate_namespace
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

    def kube(self) -> Kube:
        args = self.args
        target = Target(
            kubeconfig=getattr(args, "kubeconfig", ""),
            context=getattr(args, "context", ""),
            cli=getattr(args, "kube_cli", "kubectl"),
        )
        return Kube(target, runner=self.runner)

    def namespace(self, kube: Kube | None = None) -> str:
        """``-n``, else ``LLMD_NAMESPACE`` (the flag's default), else the kube context's namespace."""
        namespace = getattr(self.args, "namespace", "") or (kube.default_namespace() if kube else "")
        return validate_namespace(namespace)

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


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    """Which cluster and namespace a command talks to."""
    parser.add_argument(
        "-n",
        "--namespace",
        default=os.environ.get("LLMD_NAMESPACE", ""),
        help="target namespace (default: $LLMD_NAMESPACE, then the kube context's namespace)",
    )
    connection = parser.add_argument_group("connection")
    connection.add_argument("--kubeconfig", default="", help="kubeconfig file (default: kubectl's own)")
    connection.add_argument("--context", default="", help="kube context")
    connection.add_argument(
        "--kube-cli", default=os.environ.get("LLMD_KUBE_CLI", "kubectl"), choices=["kubectl", "oc"]
    )
