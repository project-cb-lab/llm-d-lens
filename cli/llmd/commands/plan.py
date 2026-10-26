"""``llmd plan``: what ``llmd standup`` would do, without changing anything."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .. import catalog, yamlio
from ..errors import PolicyError
from ..plan import Options, Plan, make_plan
from . import Context, Handler, add_source_arguments


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments every command that plans a stack takes."""
    parser.add_argument("stack", help="path of the stack in the source (see `llmd stacks`)")
    add_source_arguments(parser)
    parser.add_argument(
        "-n",
        "--namespace",
        default=os.environ.get("LLMD_NAMESPACE", ""),
        help="target namespace (env LLMD_NAMESPACE)",
    )
    parser.add_argument("--name", default="", help="instance name (default: from the stack's path)")
    parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help="acknowledge privileged containers, host namespaces, hostPath volumes and SCC bindings "
        "that an administrator granted to the namespace",
    )
    parser.add_argument(
        "--skip-admin-parts",
        action="store_true",
        help="leave out cluster-scoped objects and SCC bindings; an administrator applies them",
    )


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("plan", help="render, check, and show what standup would do (no changes)")
    add_plan_arguments(parser)
    parser.add_argument("--offline", action="store_true", help="do not contact the cluster")
    parser.add_argument("--manifest", default="", metavar="FILE", help="also write the planned objects here")
    parser.add_argument("--admin-yaml", default="", metavar="FILE", help="write the administrator parts here")
    return {"plan": command_plan}


def plan_from_args(ctx: Context) -> Plan:
    args = ctx.args
    stack = catalog.find_stack(ctx.source(), args.stack)
    options = Options(
        namespace=args.namespace,
        instance=args.name or catalog.default_name(stack),
        allow_privileged=args.allow_privileged,
        skip_admin_parts=args.skip_admin_parts,
    )
    return make_plan(stack, options, runner=ctx.runner)


def command_plan(ctx: Context) -> int:
    plan = plan_from_args(ctx)
    if ctx.args.manifest:
        yamlio.write_text_atomic(Path(ctx.args.manifest), yamlio.dump_documents(plan.documents))
    if ctx.args.admin_yaml:
        parts = [part["document"] for part in plan.policy.admin_parts]
        yamlio.write_text_atomic(Path(ctx.args.admin_yaml), yamlio.dump_documents(parts))
    if ctx.args.output != "text":
        ctx.emit({**plan.summary(), "documents": plan.documents})
    else:
        print_plan(plan)
    return PolicyError.exit_code if plan.blocking else 0


def print_plan(plan: Plan) -> None:
    summary = plan.summary()
    source = summary["source"]
    revision = f" @ {source['revision'][:12]}" if source["revision"] else ""
    print(f"Plan   {summary['stack']}")
    print(f"Target {summary['namespace']}/{summary['instance']}")
    print(f"Source {source['name']}{revision}{' (dirty)' if source['dirty'] else ''}")
    print(f"Render {summary['render']}")
    print(f"\nObjects ({len(summary['objects'])})")
    for item in summary["objects"]:
        print(f"  {item}")
    if summary["workloads"]:
        print("\nWorkloads")
        for item in summary["workloads"]:
            where = item["object"] + (
                "" if item["template"] in ("template", "pod") else f" {item['template']}"
            )
            pods = "?" if item["pods"] is None else item["pods"]
            extras = "".join(
                f", {resource} x{count}/pod" for resource, count in sorted(item["accelerators"].items())
            )
            print(f"  {where}: {pods} pod(s){extras}")
        if summary["accelerators"]:
            total = ", ".join(f"{count} {resource}" for resource, count in summary["accelerators"].items())
            print(f"  total: {total}")
    if summary["prerequisites"]:
        print("\nMust already exist in the namespace")
        for item in summary["prerequisites"]:
            state = item.get("state")
            print(f"  {item['kind']}/{item['name']}" + (f"  [{state}]" if state else ""))
    if summary["adminParts"]:
        print("\nNeeds an administrator (`--admin-yaml FILE` writes them for `kubectl apply -f FILE`)")
        for item in summary["adminParts"]:
            print(f"  {item['object']}  [{item['reason']}]")
    if plan.cluster:
        print("\nCluster")
        for key, value in plan.cluster.items():
            print(f"  {key}: {value}")
    if summary["findings"]:
        print("\nFindings")
        for finding in summary["findings"]:
            print(f"  {finding['severity'].upper():<8} {finding['message']}")
            if finding.get("hint"):
                print(f"           hint: {finding['hint']}")
    print(f"\nDigest {summary['digest'][:16]}")
    print(
        "Result "
        + (f"BLOCKED by {summary['blocking']} finding(s)" if summary["blocking"] else "ready to stand up")
    )
