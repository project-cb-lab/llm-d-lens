"""``llmd rollouts``: move a running instance to what another source ref renders, in place."""

from __future__ import annotations

import argparse
import sys
import time

from .. import inventory
from ..errors import PolicyError, UsageError
from ..preflight import preflight
from ..rollouts import engine
from ..rollouts.plan import RolloutPlan, plan_rollout
from . import Context, Handler, add_target_arguments
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


def _pool_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pool-label", default=POOL_LABEL, metavar="LABEL", help=argparse.SUPPRESS)


def _watch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--watch", action="store_true", help="follow the roll, and finish it when it is done")
    parser.add_argument("--timeout", type=int, default=6 * 3600, help="--watch timeout in seconds")
    parser.add_argument("--interval", type=int, default=30, help=argparse.SUPPRESS)


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("rollouts", help="roll a running instance to another source ref, in place")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    plan = actions.add_parser("plan", help="what the rollout would change, step by step (no changes)")
    add_rollout_arguments(plan)
    start = actions.add_parser("start", help="start the drainers, apply the change, and (--watch) follow it")
    add_rollout_arguments(start)
    drain = start.add_mutually_exclusive_group()
    drain.add_argument(
        "--drain",
        default="",
        metavar="COMMAND",
        help="the stack's drain command, run once per role while the roll is in progress; fields: {role} "
        "{source} {namespace} {set} {old_revision} {max_unavailable}",
    )
    drain.add_argument(
        "--no-drain",
        action="store_true",
        help="roll without draining (requests in flight on a removed group fail)",
    )
    start.add_argument(
        "--expected-digest", default="", metavar="DIGEST", help="refuse unless the plan still has it"
    )
    _watch_arguments(start)
    for name, text in (
        ("status", "where the roll stands, per role and revision"),
        ("pause", "stop the drainers; the stack's guards hold the roll where it stands"),
        ("resume", "start the drainers again on this machine"),
        ("abort", "re-apply what ran before the rollout (itself a roll; the drainers keep running)"),
        ("finish", "end the rollout once the old revision is gone"),
    ):
        action = actions.add_parser(name, help=text)
        action.add_argument("instance")
        add_target_arguments(action)
        _pool_argument(action)
        if name in ("status", "resume", "abort"):
            _watch_arguments(action)
        if name == "abort":
            action.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    return {"rollouts": command_rollouts}


def rollout_plan(ctx: Context) -> RolloutPlan:
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    name = ctx.args.name
    if not name:
        raise UsageError("pass --name: the instance to roll")
    record = inventory.require(kube, namespace, name)
    imported = inventory.management(record) == inventory.IMPORTED
    # Rendered the way the instance was deployed: an imported stack carries no llmd labels. Its
    # objects already run, so privileged findings describe them (as for an import) instead of
    # blocking; an instance llmd stood up was planned with the same acknowledgements as before.
    plan = plan_from_args(ctx, kube, label=not imported)
    preflight(kube, plan, existing="import" if imported else "rollout")
    return plan_rollout(kube, plan, floors=_floors(ctx.args.floor), pool_label=ctx.args.pool_label)


def command_rollouts(ctx: Context) -> int:
    action = ctx.args.action
    if action == "plan":
        rollout = rollout_plan(ctx)
        if ctx.args.output != "text":
            ctx.emit(rollout.summary())
        else:
            print_rollout_plan(rollout)
        return PolicyError.exit_code if rollout.blocking else 0
    if action == "start":
        return command_start(ctx)
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    instance = ctx.args.instance
    state, pool = ctx.state_dir, ctx.args.pool_label
    if action == "status":
        while True:
            current = engine.status(kube, namespace, instance, state=state, pool_label=pool)
            ctx.emit(current) if ctx.args.output != "text" else print_rollout_status(current)
            if not ctx.args.watch or current["done"] or current["phase"] in ("None", "Finished", "Aborted"):
                return 0
            time.sleep(ctx.args.interval)
    if action == "pause":
        result = engine.pause(kube, namespace, instance, state=state)
        print(
            f"Paused {instance}: stopped the {', '.join(result['stopped']) or 'no'} drainer(s). Groups the "
            "drainers had not announced stay where they are."
        )
        return 0
    if action == "resume":
        result = engine.resume(kube, namespace, instance, state=state, env=_kube_env(ctx))
        print(f"Resumed {instance}: {len(result['drainers'])} drainer(s) running")
        return _follow(ctx, kube, namespace, instance) if ctx.args.watch else 0
    if action == "abort":
        if not ctx.args.yes and not _confirm(
            f"Roll {instance} back to what it ran before the rollout? [y/N] "
        ):
            return 1
        record = inventory.require(kube, namespace, instance)
        result = engine.abort(
            kube, namespace, instance, server_side=inventory.management(record) != inventory.IMPORTED
        )
        print(
            f"Aborting {instance}: re-applied {', '.join(result['restored'])}; the controller rolls back "
            "the same way, so keep the drainers running"
        )
        return _follow(ctx, kube, namespace, instance) if ctx.args.watch else 0
    result = engine.finish(kube, namespace, instance, state=state, pool_label=pool)
    print(
        f"Rollout of {instance} {result['phase'].lower()}"
        + (f" at revision {result['revision']}" if result["revision"] else "")
    )
    return 0


def command_start(ctx: Context) -> int:
    args = ctx.args
    if args.context and args.drain:
        # The drain command runs its own oc/kubectl, which reads the kubeconfig's current context.
        raise UsageError(
            "--context is not passed to the drain command",
            hint=f"Select the cluster for both with `kubectl config use-context {args.context}`.",
        )
    rollout = rollout_plan(ctx)
    if rollout.blocking:
        print_rollout_plan(rollout) if args.output == "text" else ctx.emit(rollout.summary())
        return PolicyError.exit_code
    kube = ctx.kube()
    result = engine.start(
        kube,
        rollout,
        drain="-" if args.no_drain else args.drain,
        state=ctx.state_dir,
        expected_digest=args.expected_digest,
        actor=rollout.plan.cluster.get("user", ""),
        env=_kube_env(ctx),
    )
    if args.output != "text":
        ctx.emit(result)
    else:
        print(f"Applied {', '.join(result['applied'])} to {result['instance']} in {result['namespace']}")
        for drainer in result["drainers"]:
            print(f"  drainer {drainer['role']}: pid {drainer['pid']}, log {drainer['log']}")
        if result["rolls"]:
            follow = f"llmd rollouts status {result['instance']} --watch"
            print(f"{result['rolls']} is rolling; follow it with `{follow}`")
    if result["rolls"] and args.watch:
        return _follow(ctx, kube, rollout.plan.namespace, rollout.plan.instance)
    return 0


def _follow(ctx: Context, kube, namespace: str, instance: str) -> int:
    result = engine.watch(
        kube,
        namespace,
        instance,
        state=ctx.state_dir,
        pool_label=ctx.args.pool_label,
        timeout=ctx.args.timeout,
        interval=ctx.args.interval,
        emit=lambda line: print(f"[roll] {line}", file=sys.stderr, flush=True),
    )
    print(
        f"Rollout of {instance} {result['phase'].lower()}"
        + (f" at revision {result['revision']}" if result["revision"] else "")
    )
    return 0


def _kube_env(ctx: Context) -> dict[str, str]:
    """The drainers use the same cluster as llmd."""
    return {"KUBECONFIG": ctx.args.kubeconfig} if getattr(ctx.args, "kubeconfig", "") else {}


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        print("error: refusing without a terminal to confirm; pass -y", file=sys.stderr)
        return False
    return input(question).strip().lower() in ("y", "yes")


def print_rollout_status(current: dict) -> None:
    print(f"{current['instance']} in {current['namespace']}: rollout {current['phase']}")
    if current["set"]:
        target = (current["progress"] or {}).get("target", "?")
        print(f"  {current['set']}: revision {current['fromRevision'] or '?'} -> {target}")
    progress = current.get("progress") or {}
    for role, item in (progress.get("roles") or {}).items():
        revisions = ", ".join(
            f"{revision} {value['serving']}/{value['groups']} serving"
            + (f", {value['outOfPool']} out of pool" if value["outOfPool"] else "")
            for revision, value in item["byRevision"].items()
        )
        print(f"  {role:<10} {item['serving']}/{item['replicas']} serving  ({revisions})")
    for drainer in current["drainers"]["running"]:
        print(f"  drainer {drainer['role']} running (pid {drainer['pid']}), log {drainer['log']}")
    for drainer in current["drainers"]["exited"]:
        print(f"  drainer {drainer['role']} EXITED, log {drainer['log']}")
    if current["done"]:
        print(f"  done: finish it with `llmd rollouts finish {current['instance']}`")


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
