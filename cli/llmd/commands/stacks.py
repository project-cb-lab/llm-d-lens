"""``llmd stacks``: the stacks a source holds."""

from __future__ import annotations

import argparse

from .. import catalog
from . import Context, Handler, add_source_arguments


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("stacks", help="list the stacks of a source (no cluster access)")
    parser.add_argument(
        "prefix", nargs="?", default="", help="only stacks below this path, e.g. manifests/production"
    )
    add_source_arguments(parser)
    return {"stacks": command_stacks}


def command_stacks(ctx: Context) -> int:
    source = ctx.source()
    stacks = catalog.list_stacks(source, ctx.runner, ctx.args.prefix)
    if ctx.args.output != "text":
        ctx.emit({"source": source.describe(), "stacks": [stack.id for stack in stacks]})
        return 0
    revision = f" @ {source.revision[:12]}" if source.revision else ""
    print(f"{source.name}{revision}{' (dirty)' if source.dirty else ''}: {len(stacks)} stack(s)")
    for stack in stacks:
        print(f"  {stack.id:<64} instance name {catalog.default_name(stack)}")
    return 0
