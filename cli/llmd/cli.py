"""llmd: plan, stand up, roll out, and remove curvebender-tools stacks inside one namespace.

A stack is a directory of a curvebender-tools checkout (a kustomize overlay or a manifest
directory). `llmd plan` shows what `llmd standup` would change, without changing anything.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .commands import Context, Handler
from .commands import plan as plan_command
from .commands import render as render_command
from .commands import sources as sources_command
from .commands import stacks as stacks_command
from .commands import standup as standup_command
from .commands import status as status_command
from .commands import teardown as teardown_command
from .errors import LlmdError

EXAMPLES = """\
examples:
  llmd sources add cb https://github.com/project-cb-lab/curvebender-tools.git#main
  llmd sources add cb-local /path/to/curvebender-tools
  llmd sources list
  llmd stacks manifests/production                             # the stacks of the source
  llmd render manifests/production/<model>/<cluster>           # the objects, as `oc apply -k` sends them
  llmd plan manifests/production/<model>/<cluster> -n <namespace> --offline
  llmd plan manifests/production/<model>/<cluster>             # namespace from the kube context
  llmd standup manifests/production/<model>/<cluster> --expected-digest <digest from plan> --wait
  llmd list
  llmd status <instance>
  llmd teardown <instance>
"""

#: Modules that add subcommands, in the order ``--help`` lists them.
COMMAND_MODULES = [
    sources_command,
    stacks_command,
    render_command,
    plan_command,
    standup_command,
    status_command,
    teardown_command,
]


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, Handler]]:
    parser = argparse.ArgumentParser(
        prog="llmd",
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"llmd {__version__}")
    parser.add_argument("-o", "--output", choices=["text", "json", "yaml"], default="text")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("LLMD_STATE_DIR", ""),
        help="local state directory: registered sources (env LLMD_STATE_DIR)",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    handlers: dict[str, Handler] = {}
    for module in COMMAND_MODULES:
        handlers.update(module.register(commands))
    return parser, handlers


def main(argv: list[str] | None = None) -> int:
    parser, handlers = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.command:
        parser.print_help()
        return 2
    try:
        return handlers[args.command](Context(args))
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
