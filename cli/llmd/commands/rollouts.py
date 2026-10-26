"""``llmd rollouts``: move a running instance to what another source ref renders, in place."""

from __future__ import annotations

import argparse

from .. import inventory
from ..errors import PolicyError, UsageError
from ..rollouts.plan import RolloutPlan, plan_rollout
from . import Context, Handler
from .plan import add_plan_arguments, plan_from_args

POOL_LABEL = "ops.llm-d.ai/production-released"


def _floors(items: list[str]) -> dict[str, int]:
    floors = {}
    for item in items or []:
        role, separator, count = item.partition("=")
        if not separator or not role or not count.isdigit():
            raise UsageError(f"--floor expects ROLE=GROUPS, got {item!r}")
        floors[role] = int(count)
    return floors


def add_rollout_arguments(parser: argparse.ArgumentParser) -> None:
    add_plan_arguments(parser)
    parser.add_argument(
        "--floor",
        action="append",
        default=[],
        metavar="ROLE=GROUPS",
        help="groups of ROLE that must keep serving during every step (repeatable)",
    )
    parser.add_argument(
        "--pool-label",
        default=POOL_LABEL,
        metavar="LABEL",
        help=f"pod label that is 'true' while a group is in the router's pool (default: {POOL_LABEL}; "
        "'' when the stack has none)",
    )


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("rollouts", help="roll a running instance to another source ref, in place")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    plan = actions.add_parser("plan", help="what the rollout would change, step by step (no changes)")
    add_rollout_arguments(plan)
    return {"rollouts": command_rollouts}


def rollout_plan(ctx: Context) -> RolloutPlan:
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    name = ctx.args.name
    if not name:
        raise UsageError("pass --name: the instance to roll")
    record = inventory.require(kube, namespace, name)
    # Rendered the way the instance was deployed: an imported stack carries no llmd labels.
    plan = plan_from_args(ctx, kube, label=inventory.management(record) != inventory.IMPORTED)
    return plan_rollout(kube, plan, floors=_floors(ctx.args.floor), pool_label=ctx.args.pool_label)


def command_rollouts(ctx: Context) -> int:
    rollout = rollout_plan(ctx)
    if ctx.args.output != "text":
        ctx.emit(rollout.summary())
    else:
        print_rollout_plan(rollout)
    return PolicyError.exit_code if rollout.blocking else 0


def print_rollout_plan(rollout: RolloutPlan) -> None:
    summary = rollout.summary()
    source, target = summary["from"], summary["to"]
    print(f"Rollout {summary['instance']} in {summary['namespace']} ({summary['management'].get('mode')})")
    print(f"  stack {summary['stack']}")
    print(f"  from  {source.get('name', '?')} @ {(source.get('revision') or '?')[:12]}")
    dirty = " (dirty)" if target["dirty"] else ""
    print(f"  to    {target['name']} @ {(target['revision'] or '?')[:12]}{dirty}")
    print(f"\nObjects: {len(summary['objects'])} to change, {summary['unchanged']} unchanged")
    for ref, state in summary["objects"].items():
        print(f"  {state:<8} {ref}")
    for ref in summary["leftAlone"]:
        print(f"  kept     {ref} (no longer rendered; a rollout never deletes)")
    for item in summary["sets"]:
        print(f"\n{item['name']}: {item['change']}")
        for role in item["roles"]:
            before, after = role["replicas"]
            print(
                f"  {role['role']:<10} {before} -> {after} group(s) of {role['podsPerGroup']} pod(s), "
                f"maxUnavailable {role['maxUnavailable']}, maxSurge {role['maxSurge']}"
            )
        if item["change"] == "roll":
            serving = ", ".join(f"{role} {count}" for role, count in item["servingDuringAStep"].items())
            print(f"  {item['steps']} step(s); every group is replaced; serving during a step: {serving}")
        for path in item["differences"][:12]:
            print(f"    changed {path}")
        if len(item["differences"]) > 12:
            print(f"    ... {len(item['differences']) - 12} more")
    if summary["findings"]:
        print("\nFindings")
        for finding in summary["findings"]:
            print(f"  {finding['severity'].upper():<8} {finding['message']}")
            if finding.get("hint"):
                print(f"           hint: {finding['hint']}")
    print(f"\nDigest {summary['digest'][:16]}")
    print(
        "Result "
        + (f"BLOCKED by {summary['blocking']} finding(s)" if summary["blocking"] else "ready to start")
    )
