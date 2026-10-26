"""``llmd sources``: register the checkouts and Git URLs stacks come from."""

from __future__ import annotations

import argparse

from . import Context, Handler


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("sources", help="register where stacks come from")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    add = actions.add_parser("add", help="register a local checkout or a Git URL")
    add.add_argument("name")
    add.add_argument("location", help="a local directory, or a Git URL (URL#REF pins a branch, tag, or ref)")
    add.add_argument("--ref", default="", help="branch, tag, commit, or ref of a Git URL")
    actions.add_parser("list", help="list registered sources")
    remove = actions.add_parser("remove", help="forget a source (the checkout is left alone)")
    remove.add_argument("name")
    return {"sources": command_sources}


def command_sources(ctx: Context) -> int:
    args = ctx.args
    if args.action == "add":
        entry = ctx.registry.add(args.name, args.location, ref=args.ref)
        ref = f"#{entry['ref']}" if entry.get("ref") else ""
        print(f"Registered source {entry['name']} -> {entry['location']}{ref}")
        return 0
    if args.action == "remove":
        ctx.registry.remove(args.name)
        print(f"Removed source {args.name}")
        return 0
    entries = ctx.registry.entries()
    if args.output != "text":
        ctx.emit(entries)
        return 0
    if not entries:
        print("No sources registered. Add one with `llmd sources add NAME PATH|URL`.")
    for entry in entries:
        ref = f"#{entry['ref']}" if entry.get("ref") else ""
        print(f"{entry['name']:<16} {entry['location']}{ref}")
    return 0
