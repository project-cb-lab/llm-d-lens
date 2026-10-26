"""llmd: plan, stand up, roll out, and remove curvebender-tools stacks inside one namespace.

A stack is a directory of a curvebender-tools checkout (a kustomize overlay or a manifest
directory). `llmd plan` shows what `llmd standup` would change, without changing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from . import __version__, yamlio
from .errors import LlmdError

EXAMPLES = """\
examples:
  llmd --version
"""

#: command name -> handler(args) returning the exit code. Each command module registers itself.
Command = Callable[[argparse.Namespace], int]


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, Command]]:
    parser = argparse.ArgumentParser(
        prog="llmd",
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"llmd {__version__}")
    parser.add_argument("-o", "--output", choices=["text", "json", "yaml"], default="text")
    parser.add_subparsers(dest="command", metavar="COMMAND")
    return parser, {}


def emit_value(value: Any, output: str) -> None:
    if output == "json":
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        print(yamlio.dump(value), end="")


def main(argv: list[str] | None = None) -> int:
    parser, commands = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.command:
        parser.print_help()
        return 2
    try:
        return commands[args.command](args)
    except LlmdError as error:
        print(f"error: {error.message}", file=sys.stderr)
        if error.hint:
            print(f"hint: {error.hint}", file=sys.stderr)
        if isinstance(error.details, list):
            for item in error.details[:20]:
                if isinstance(item, dict) and item.get("message"):
                    print(f"  - {item['message']}", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
