"""``status`` and ``list``: what an instance runs, read from the cluster.

The same code serves instances llmd stood up and instances it imported: objects are found
by the name and UID recorded in the instance's inventory, and pods through each
workload's own selector.
"""

from __future__ import annotations

import time
from typing import Any

from . import health, inventory
from .errors import NotReadyError
from .kube import Kube
from .policy import pod_templates, template_accelerators


def instance_status(kube: Kube, namespace: str, instance: str) -> dict[str, Any]:
    record = inventory.require(kube, namespace, instance)
    objects, problems, endpoints = [], [], []
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
            problems += health.pod_problems(pods)
        if live and item["kind"] == "Service":
            for port in (live.get("spec") or {}).get("ports") or []:
                endpoints.append(f"http://{item['name']}.{namespace}.svc.cluster.local:{port.get('port')}")
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
