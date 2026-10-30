"""``llmd standup``: apply a stack to an empty namespace (or update the instance llmd stood up)."""

from __future__ import annotations

import argparse
import sys

from .. import lifecycle
from ..errors import PolicyError
from ..preflight import preflight
from ..status import wait_ready
from . import Context, Handler
from .plan import add_plan_arguments, plan_from_args, print_plan


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser(
        "standup", help="apply a stack to the namespace and record it as an instance"
    )
    add_plan_arguments(parser)
    parser.add_argument(
        "--expected-digest",
        default="",
        metavar="DIGEST",
        help="refuse unless the plan still has this digest (from a reviewed `llmd plan`)",
    )
    parser.add_argument("--wait", action="store_true", help="wait until every object and pod is Ready")
    parser.add_argument("--timeout", type=int, default=1800, help="--wait timeout in seconds")
    return {"standup": command_standup}


def command_standup(ctx: Context) -> int:
    kube = ctx.kube()
    plan = plan_from_args(ctx, kube)
    preflight(kube, plan)
    if plan.blocking:
        if ctx.args.output == "text":
            print_plan(plan)
        else:
            ctx.emit(plan.summary())
        return PolicyError.exit_code
    result = lifecycle.standup(
        kube, plan, expected_digest=ctx.args.expected_digest, actor=plan.cluster.get("user", "")
    )
    if ctx.args.wait:
        result["status"] = wait_ready(
            kube,
            plan.namespace,
            plan.instance,
            timeout=ctx.args.timeout,
            emit=lambda line: print(f"[wait] {line}", file=sys.stderr, flush=True),
        )
    if ctx.args.output != "text":
        ctx.emit(result)
        return 0
    where = f"{result['instance']} in {result['namespace']}"
    print(f"Stood up {where} ({result['applied']} objects, digest {result['digest'][:12]})")
    if result.get("switchedFrom"):
        print(f"  moved from stack {result['switchedFrom']}")
    for item in result["pruned"]:
        print(f"  pruned {item} (no longer in the stack)")
    print(f"Follow it with: llmd status {result['instance']} -n {result['namespace']}")
    return 0
