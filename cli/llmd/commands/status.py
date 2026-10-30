"""``llmd status`` and ``llmd list``."""

from __future__ import annotations

import argparse
import time

from ..status import instance_status, list_instances
from . import Context, Handler, add_target_arguments


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    status = commands.add_parser("status", help="show an instance's objects, pods and health")
    status.add_argument("instance")
    status.add_argument("--watch", action="store_true", help="refresh every 10 seconds")
    add_target_arguments(status)
    listing = commands.add_parser("list", help="the instances in the namespace")
    add_target_arguments(listing)
    return {"status": command_status, "list": command_list}


def command_status(ctx: Context) -> int:
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    while True:
        status = instance_status(kube, namespace, ctx.args.instance)
        if ctx.args.output != "text":
            ctx.emit(status)
        else:
            print_status(status)
        if not ctx.args.watch:
            return 0
        time.sleep(10)
        print()


def print_status(status: dict) -> None:
    mode = status["management"].get("mode")
    owner = f", imported ({status['management'].get('manager')})" if mode == "imported" else ""
    pods = status["pods"]
    counts = f"pods {pods['ready']}/{pods['total']} ready{owner}"
    print(f"{status['instance']} in {status['namespace']}: {status['health']} ({counts})")
    print(
        f"  stack {status['stack']}"
        + (f" (moved from {status['previousStack']})" if status.get("previousStack") else "")
    )
    for item in status["objects"]:
        print(f"  {item['state']:<12} {item['object']}" + (f"  {item['detail']}" if item["detail"] else ""))
    for problem in pods["problems"][:10]:
        seen = f" (event, last seen {problem['lastSeen']})" if problem.get("lastSeen") else ""
        where = f" container {problem['container']}" if problem.get("container") else ""
        exited = f", exit code {problem['exitCode']}" if problem.get("exitCode") else ""
        line = f"  pod {problem['pod']}{where}: {problem['reason']}{exited} {problem['message']}"
        print(line.rstrip() + seen)
        if problem.get("log"):
            print("    last lines before it exited:")
            for text in problem["log"].splitlines():
                print(f"      {text}")
    for endpoint in status["endpoints"]:
        print(f"  endpoint {endpoint}")
    if status.get("unreadable"):
        print(
            f"  could not read {len(status['unreadable'])} part(s) with your permissions in "
            f"{status['namespace']}; health is from what could be read"
        )


def command_list(ctx: Context) -> int:
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    instances = list_instances(kube, namespace)
    if ctx.args.output != "text":
        ctx.emit({"namespace": namespace, "instances": instances})
        return 0
    if not instances:
        print(f"No instances in {namespace}.")
        return 0
    print(f"{'INSTANCE':<32} {'MODE':<9} {'PHASE':<10} {'PODS':<9} {'GPUS':<6} STACK")
    for item in instances:
        if item.get("phase") == "Invalid":
            print(f"{item['instance']:<32} {'?':<9} Invalid    {item.get('error', '')}")
            continue
        pods = f"{item['pods']['ready']}/{item['pods']['total']}"
        gpus = (
            sum(count for resource, count in item["accelerators"].items() if resource.endswith("/gpu")) or "-"
        )
        columns = f"{item['instance']:<32} {item['management']:<9} {item['phase']:<10} {pods:<9} {gpus!s:<6}"
        print(f"{columns} {item['stack']}")
        if item.get("unreadable"):
            print(f"  {len(item['unreadable'])} object(s) could not be read; `llmd status` names them")
    print(f"\nDetails: llmd status INSTANCE -n {namespace}")
    return 0
