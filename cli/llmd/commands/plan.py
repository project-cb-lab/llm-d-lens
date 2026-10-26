"""``llmd plan``: what ``llmd standup`` would do, without changing anything."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import catalog, yamlio
from ..errors import PolicyError
from ..kube import Kube
from ..plan import Options, Plan, make_plan
from ..policy import fallback_scope
from ..preflight import preflight
from . import Context, Handler, add_source_arguments, add_target_arguments


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments every command that plans a stack takes."""
    parser.add_argument("stack", help="path of the stack in the source (see `llmd stacks`)")
    add_source_arguments(parser)
    add_target_arguments(parser)
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


def plan_from_args(ctx: Context, kube: Kube | None, *, label: bool = True) -> Plan:
    """The plan the command line asks for. With ``kube``, API discovery says which kinds are
    cluster-scoped and the namespace may come from the kube context; without, the plan is offline."""
    args = ctx.args
    stack = catalog.find_stack(ctx.source(), args.stack)
    options = Options(
        namespace=ctx.namespace(kube),
        instance=args.name or catalog.default_name(stack),
        allow_privileged=args.allow_privileged,
        skip_admin_parts=args.skip_admin_parts,
    )
    scope = fallback_scope if kube is None else _discovered_scope(kube)
    return make_plan(stack, options, runner=ctx.runner, scope=scope, label=label)


def _discovered_scope(kube: Kube):
    def scope(api_version: str, kind: str) -> bool | None:
        served = kube.scope(api_version, kind)
        return fallback_scope(api_version, kind) if served is None else served

    return scope


def command_plan(ctx: Context) -> int:
    kube = None if ctx.args.offline else ctx.kube()
    plan = plan_from_args(ctx, kube)
    if kube is not None:
        preflight(kube, plan)
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
