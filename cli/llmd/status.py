"""``status`` and ``list``: what an instance runs, read from the cluster.

The same code serves instances llmd stood up and instances it imported: objects are found
by the name and UID recorded in the instance's inventory, and pods through each
workload's own selector.

A pod that is not Ready is explained from its own status first. A Pending pod whose status
says nothing (stuck in ContainerCreating on a volume that does not mount, say) gets its
latest Warning event, from one read of the namespace's pod events. A container that crashed
and is restarting gets the last lines it wrote, for one pod per workload (its replicas run the
same arguments). Both reads are best effort: without permission, status shows what the pods
say.
"""

from __future__ import annotations

import time
from typing import Any

from . import health, inventory
from .errors import CommandError, NotReadyError
from .kube import Kube
from .policy import pod_templates, template_accelerators


def instance_status(kube: Kube, namespace: str, instance: str) -> dict[str, Any]:
    record = inventory.require(kube, namespace, instance)
    objects, problems, endpoints, seen = [], [], [], []
    pods_ready = pods_total = 0
    for item in record.get("objects") or []:
        live = kube.get(item["resource"], item["name"], namespace)
        if live is not None and item.get("uid") and live["metadata"].get("uid") != item["uid"]:
            objects.append(
                {
                    "object": f"{item['kind']}/{item['name']}",
                    "state": "replaced",
                    "detail": "deleted and created again outside llmd",
                }
            )
            continue
        selector = health.pod_selector(live) if live else ""
        pods = kube.get_json("pods", namespace, selector=selector) if selector else []
        state, detail = health.evaluate(live, pods)
        objects.append({"object": f"{item['kind']}/{item['name']}", "state": state, "detail": detail})
        if selector:
            pods_total += sum(1 for pod in pods if not (pod.get("metadata") or {}).get("deletionTimestamp"))
            pods_ready += sum(1 for pod in pods if health.pod_ready(pod))
            found = health.pod_problems(pods)
            _add_crash_log(kube, namespace, found)
            problems += found
            seen += pods
        if live and item["kind"] == "Service":
            for port in (live.get("spec") or {}).get("ports") or []:
                endpoints.append(f"http://{item['name']}.{namespace}.svc.cluster.local:{port.get('port')}")
    problems += _pending_warnings(kube, namespace, seen, problems)
    return {
        "instance": instance,
        "namespace": namespace,
        "stack": (record.get("stack") or {}).get("id"),
        "management": record.get("management") or {"mode": inventory.MANAGED},
        "phase": record.get("phase"),
        "health": health.overall(objects, problems),
        "objects": objects,
        "pods": {"ready": pods_ready, "total": pods_total, "problems": problems},
        "endpoints": endpoints,
    }


#: Waiting reasons of a container that ran and exited, whose previous log says why.
CRASH_REASONS = {"CrashLoopBackOff", "RunContainerError"}
#: Log lines shown for a crashed container.
CRASH_LOG_LINES = 8


def _add_crash_log(kube: Kube, namespace: str, problems: list[dict[str, str]]) -> None:
    """The last lines the first crashed container of one workload wrote before it exited."""
    for problem in problems:
        if problem["reason"] in CRASH_REASONS and problem.get("container"):
            log = kube.previous_log(problem["pod"], problem["container"], namespace, lines=CRASH_LOG_LINES)
            lines = [line.rstrip() for line in (log or "").splitlines() if line.strip()]
            if lines:
                problem["log"] = "\n".join(lines[-CRASH_LOG_LINES:])[-1500:]
            return


#: The pod's PodScheduled condition already says why it is not scheduled, and stays current.
_COVERED_EVENTS = {"FailedScheduling"}


def _event_time(event: dict[str, Any]) -> str:
    series = event.get("series") or {}
    return str(
        event.get("lastTimestamp")
        or series.get("lastObservedTime")
        or event.get("eventTime")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )


def _pending_warnings(
    kube: Kube, namespace: str, pods: list[dict[str, Any]], problems: list[dict[str, str]]
) -> list[dict[str, str]]:
    """The latest Warning event of each Pending pod that no problem explains yet."""
    explained = {problem["pod"] for problem in problems}
    quiet = {
        (pod.get("metadata") or {}).get("name", "")
        for pod in pods
        if (pod.get("status") or {}).get("phase") == "Pending"
        and not (pod.get("metadata") or {}).get("deletionTimestamp")
    } - explained
    if not quiet:
        return []
    try:
        events = kube.get_json("events", namespace, field_selector="involvedObject.kind=Pod,type=Warning")
    except CommandError:
        return []
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        name = (event.get("involvedObject") or {}).get("name", "")
        if name in quiet and event.get("reason") not in _COVERED_EVENTS:
            if _event_time(event) >= _event_time(latest.get(name, {})):
                latest[name] = event
    return [
        {
            "pod": name,
            "reason": event.get("reason") or "Warning",
            "message": (event.get("message") or "")[:300],
            "lastSeen": _event_time(event),
        }
        for name, event in sorted(latest.items())
    ]


def wait_ready(
    kube: Kube, namespace: str, instance: str, *, timeout: float, interval: float = 10, emit=None
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        status = instance_status(kube, namespace, instance)
        summary = f"{status['health']}: pods {status['pods']['ready']}/{status['pods']['total']} ready"
        if emit and summary != last:
            emit(summary)
            last = summary
        if status["health"] == "Ready":
            return status
        if time.monotonic() >= deadline:
            raise NotReadyError(
                f"instance {instance} was not ready after {timeout:.0f}s ({summary})",
                hint=f"The cluster keeps converging; follow it with `llmd status {instance} -n {namespace}`.",
                details=status["pods"]["problems"],
            )
        time.sleep(interval)


def list_instances(kube: Kube, namespace: str) -> list[dict[str, Any]]:
    """Every instance in the namespace, with its pods and the GPUs its workloads ask for."""
    listed = []
    for record in inventory.list_records(kube, namespace):
        if record.get("phase") == "Invalid":
            listed.append(record)
            continue
        ready = total = 0
        accelerators: dict[str, int] = {}
        for item in record.get("objects") or []:
            live = kube.get(item["resource"], item["name"], namespace)
            selector = health.pod_selector(live) if live else ""
            if not selector:
                continue
            pods = kube.get_json("pods", namespace, selector=selector)
            total += sum(1 for pod in pods if not (pod.get("metadata") or {}).get("deletionTimestamp"))
            ready += sum(1 for pod in pods if health.pod_ready(pod))
            for _, template, count in pod_templates(live):
                for resource, amount in template_accelerators(template).items():
                    accelerators[resource] = accelerators.get(resource, 0) + amount * (count or 0)
        listed.append(
            {
                "instance": record["instance"],
                "stack": (record.get("stack") or {}).get("id"),
                "management": (record.get("management") or {}).get("mode") or inventory.MANAGED,
                "phase": record.get("phase"),
                "pods": {"ready": ready, "total": total},
                "accelerators": accelerators,
            }
        )
    return listed
