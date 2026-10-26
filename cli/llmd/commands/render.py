"""``llmd render``: the stack's objects as the team's own ``oc apply`` would send them."""

from __future__ import annotations

import argparse
import sys

from .. import catalog, yamlio
from ..render import render
from . import Context, Handler, add_source_arguments


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("render", help="print a stack's rendered objects (no cluster access)")
    parser.add_argument("stack", help="path of the stack in the source (see `llmd stacks`)")
    add_source_arguments(parser)
    return {"render": command_render}


def command_render(ctx: Context) -> int:
    stack = catalog.find_stack(ctx.source(), ctx.args.stack)
    rendered = render(stack, runner=ctx.runner)
    print(f"[render] {rendered.command}: {len(rendered.documents)} object(s)", file=sys.stderr)
    if ctx.args.output == "json":
        ctx.emit(rendered.documents)
    else:
        print(yamlio.dump_documents(rendered.documents), end="")
    return 0
