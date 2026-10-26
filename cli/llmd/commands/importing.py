"""``llmd import``: record a stack deployed with ``oc apply`` or Helm, changing nothing."""

from __future__ import annotations

import argparse

from ..errors import PolicyError
from ..importing import compare, import_stack
from ..preflight import preflight
from . import Context, Handler
from .plan import add_plan_arguments, plan_from_args, print_plan


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser(
        "import",
        help="record a running stack (deployed with `oc apply` or Helm) as an instance; nothing else changes",
    )
    add_plan_arguments(parser)
    parser.add_argument(
        "--manager",
        default="kubectl",
        choices=["kubectl", "helm", "other"],
        help="the workflow that deployed the stack (default: kubectl, for `oc apply -k`)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only check that the stack's objects run and match the files"
    )
    return {"import": command_import}


def command_import(ctx: Context) -> int:
    kube = ctx.kube()
    # Imported objects are never labelled, so they are planned without llmd's labels.
    plan = plan_from_args(ctx, kube, label=False)
    preflight(kube, plan, existing="import")
    report = compare(kube, plan)
    missing = [item for item in report if item["state"] == "missing"]
    drifted = [item for item in report if item["state"] == "drifted"]
    if ctx.args.dry_run or missing:
        if ctx.args.output != "text":
            ctx.emit({**plan.summary(), "comparison": report})
        else:
            print_plan(plan)
            _print_comparison(report)
        return PolicyError.exit_code if missing else 0
    result = import_stack(kube, plan, manager=ctx.args.manager, actor=plan.cluster.get("user", ""))
    result["drifted"] = [item["object"] for item in drifted]
    if ctx.args.output != "text":
        ctx.emit(result)
        return 0
    where = f"{result['instance']} in {result['namespace']}"
    print(f"Imported {where}: {result['imported']} running objects recorded; nothing on them was changed")
    for item in drifted:
        where = ", ".join(item["differences"][:4])
        print(f"  warning: {item['object']} differs from the stack's files at {where}")
    print(f"Follow it with: llmd status {result['instance']} -n {result['namespace']}")
    return 0


def _print_comparison(report: list[dict]) -> None:
    print("\nRunning objects compared with the stack's files")
    for item in report:
        detail = f"  ({', '.join(item['differences'][:4])})" if item["differences"] else ""
        print(f"  {item['state']:<9} {item['object']}{detail}")
