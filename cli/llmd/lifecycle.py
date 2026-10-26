"""Stand an instance up, and tear it down, in one namespace.

Standup writes the instance's record *before* it applies anything, so an interrupted
standup can still be torn down, then applies the planned objects with server-side apply.
A later standup of the same instance updates its objects and prunes the ones the stack no
longer holds. Teardown deletes exactly the recorded objects whose UID still matches;
PersistentVolumeClaims are kept unless asked for.
"""

from __future__ import annotations

from typing import Any

from . import inventory
from .errors import ConflictError, PolicyError
from .kube import Kube
from .model import INSTANCE_LABEL, MANAGED_BY_LABEL, MANAGED_BY_VALUE
from .plan import Plan
from .preflight import resources

KEPT_KINDS = {"PersistentVolumeClaim"}


def standup(kube: Kube, plan: Plan, *, expected_digest: str = "", actor: str = "") -> dict[str, Any]:
    if plan.blocking:
        raise PolicyError(
            f"the plan has {len(plan.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in plan.blocking],
            hint="Run `llmd plan` to see them.",
        )
    if expected_digest and expected_digest != plan.digest:
        raise ConflictError(
            "the stack or the cluster changed since the plan was reviewed",
            hint=f"Review `llmd plan` again; it now has digest {plan.digest[:16]}.",
        )
    previous = inventory.read(kube, plan.namespace, plan.instance)
    if previous and inventory.management(previous) == inventory.IMPORTED:
        raise PolicyError(
            f"instance {plan.instance} was imported; standup never takes over another workflow's objects",
            hint="Change it with `llmd rollouts`.",
        )
    if previous and previous["stack"]["id"] != plan.stack.id:
        raise ConflictError(
            f"instance {plan.instance} runs stack {previous['stack']['id']}, not {plan.stack.id}",
            hint="Tear it down first, or stand this stack up under another --name.",
        )
    names = resources(kube, plan)
    planned = [inventory.object_entry(doc, name) for doc, name in zip(plan.documents, names, strict=True)]
    planned_keys = {(item["kind"], item["name"]) for item in planned}
    stale = [
        item
        for item in (previous or {}).get("objects") or []
        if (item["kind"], item["name"]) not in planned_keys
    ]
    record = {
        "schema": inventory.SCHEMA,
        "instance": plan.instance,
        "namespace": plan.namespace,
        "management": {"mode": inventory.MANAGED},
        "phase": "Applying",
        "stack": {"id": plan.stack.id, "source": plan.stack.source.describe()},
        "planDigest": plan.digest,
        # Recorded before applying, with what may still be there from the last standup.
        "objects": planned + stale,
        "createdAt": (previous or {}).get("createdAt") or inventory.now(),
        "updatedBy": actor,
    }
    inventory.write(kube, record)
    applied = kube.apply(plan.documents, plan.namespace)
    uids = {(item["kind"], item["metadata"]["name"]): item["metadata"].get("uid", "") for item in applied}
    pruned = _prune(kube, plan.namespace, plan.instance, stale)
    record["objects"] = [{**item, "uid": uids.get((item["kind"], item["name"]), "")} for item in planned] + [
        item for item in stale if f"{item['kind']}/{item['name']}" not in pruned
    ]
    record["phase"] = "Applied"
    inventory.write(kube, record)
    return {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "applied": len(applied),
        "pruned": pruned,
        "digest": plan.digest,
    }


def _owned(live: dict[str, Any] | None, instance: str) -> bool:
    labels = ((live or {}).get("metadata") or {}).get("labels") or {}
    return labels.get(INSTANCE_LABEL) == instance and labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE


def _prune(kube: Kube, namespace: str, instance: str, stale: list[dict[str, Any]]) -> list[str]:
    """Delete what the stack no longer holds, if it is still this instance's. Claims are kept."""
    pruned = []
    for item in stale:
        if item["kind"] in KEPT_KINDS:
            continue
        live = kube.get(item["resource"], item["name"], namespace)
        if _owned(live, instance):
            kube.delete(item["resource"], item["name"], namespace)
            pruned.append(f"{item['kind']}/{item['name']}")
    return pruned
