"""``llmd teardown``: remove exactly what an instance owns."""

from __future__ import annotations

import argparse
import sys

from .. import inventory, lifecycle
from . import Context, Handler, add_target_arguments


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("teardown", help="remove exactly what an instance owns, and its record")
    parser.add_argument("instance")
    parser.add_argument("--delete-volumes", action="store_true", help="also delete the instance's claims")
    parser.add_argument(
        "--forget",
        action="store_true",
        help="delete only the instance's record; every object stays as it is (for an imported stack)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    add_target_arguments(parser)
    return {"teardown": command_teardown}


def command_teardown(ctx: Context) -> int:
    args = ctx.args
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    record = inventory.require(kube, namespace, args.instance)
    imported = inventory.management(record) == inventory.IMPORTED
    if args.forget:
        question = f"Stop managing {args.instance} in {namespace}? Its objects are left running. [y/N] "
    else:
        owner = f" (deployed by {record['management'].get('manager')}, imported)" if imported else ""
        count = len(record["objects"])
        question = f"Delete instance {args.instance}{owner} and its {count} objects from {namespace}? [y/N] "
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing to tear down without a terminal to confirm; pass -y", file=sys.stderr)
            return 2
        if input(question).strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    result = lifecycle.teardown(
        kube, namespace, args.instance, delete_volumes=args.delete_volumes, forget=args.forget
    )
    if args.output != "text":
        ctx.emit(result)
        return 0
    if result.get("forgot"):
        print(f"Stopped managing {args.instance}; no object was changed")
        return 0
    print(f"Removed {len(result['deleted'])} object(s) of {args.instance} from {namespace}")
    for item in result["kept"]:
        print(f"  kept {item} (--delete-volumes removes it)")
    for item in result["skipped"]:
        print(f"  skipped {item}: it was replaced or taken over since standup")
    return 0
