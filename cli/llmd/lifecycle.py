"""Stand an instance up, and tear it down, in one namespace.

Standup writes the instance's record *before* it applies anything, so an interrupted
standup can still be torn down, then applies the planned objects with server-side apply.
A later standup of the same instance updates its objects and prunes the ones the stack no
longer holds. Teardown deletes exactly the recorded objects whose UID still matches;
PersistentVolumeClaims are kept unless asked for.
"""

from __future__ import annotations

from typing import Any

from . import inventory, yamlio
from .errors import ConflictError, PolicyError
from .kube import Kube
from .model import INSTANCE_LABEL, KEPT_KINDS, MANAGED_BY_LABEL, MANAGED_BY_VALUE
from .plan import Plan
from .preflight import resources


def standup(kube: Kube, plan: Plan, *, expected_digest: str = "", actor: str = "") -> dict[str, Any]:
    if plan.blocking:
        raise PolicyError(
            f"the plan has {len(plan.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in plan.blocking],
            hint="Run `llmd plan` to see them.",
        )
    if expected_digest and not yamlio.digest_matches(expected_digest, plan.digest):
        raise ConflictError(
            "the stack or the cluster changed since the plan was reviewed",
            hint=f"Review `llmd plan` again; it now has digest {plan.digest[: yamlio.SHORT_DIGEST]}.",
        )
    previous = inventory.read(kube, plan.namespace, plan.instance)
    if previous and inventory.management(previous) == inventory.IMPORTED:
        raise PolicyError(
            f"instance {plan.instance} was imported; standup never takes over another workflow's objects",
            hint="Change it with `llmd rollouts`.",
        )
    switched = bool(previous) and previous["stack"]["id"] != plan.stack.id
    if switched and not plan.options.switch_stack:
        raise ConflictError(
            f"instance {plan.instance} runs stack {previous['stack']['id']}, not {plan.stack.id}",
            hint="Pass --switch-stack to move it to this stack in place, tear it down first, or stand this "
            "stack up under another --name.",
        )
    if switched and (previous.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(
            f"instance {plan.instance} has a rollout in progress; it cannot move to another stack now",
            hint="Finish or abort the rollout first.",
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
        **({"previousStack": previous["stack"]} if switched else _kept_previous_stack(previous)),
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
        **({"switchedFrom": previous["stack"]["id"]} if switched else {}),
    }


def _kept_previous_stack(previous: dict[str, Any] | None) -> dict[str, Any]:
    """A later standup of the same stack keeps the record of the stack the instance came from."""
    return {"previousStack": previous["previousStack"]} if (previous or {}).get("previousStack") else {}


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


def teardown(
    kube: Kube, namespace: str, instance: str, *, delete_volumes: bool = False, forget: bool = False
) -> dict[str, Any]:
    """Delete what the instance owns, newest first, and its record.

    An object is deleted only while its UID is the recorded one (and, for objects llmd applied,
    while it still carries the instance's labels); anything else is skipped and reported.
    ``forget`` deletes only the record: the objects stay as they are, unmanaged."""
    record = inventory.require(kube, namespace, instance)
    imported = inventory.management(record) == inventory.IMPORTED
    if forget:
        kube.delete("configmaps", inventory.inventory_name(instance), namespace)
        return {
            "instance": instance,
            "namespace": namespace,
            "deleted": [],
            "kept": [],
            "skipped": [],
            "forgot": True,
        }
    targets = [item for item in record["objects"] if delete_volumes or item["kind"] not in KEPT_KINDS]
    kept = [item for item in record["objects"] if item not in targets]
    names = sorted({item["resource"] for item in targets} | {"configmaps"})
    answers = kube.access_reviews([("delete", *_split(name), namespace) for name in names])
    denied = [name for name, allowed in zip(names, answers, strict=True) if allowed is False]
    if denied:
        raise PolicyError(
            f"you cannot delete {', '.join(denied)} in {namespace}; nothing was removed",
            hint="Ask a namespace administrator for the missing permission.",
        )
    inventory.write(kube, {**record, "phase": "Deleting"})
    deleted, skipped = [], []
    for item in reversed(targets):
        ref = f"{item['kind']}/{item['name']}"
        live = kube.get(item["resource"], item["name"], namespace)
        if live is None:
            continue
        same = not item.get("uid") or live["metadata"].get("uid") == item["uid"]
        if not same or (not imported and not _owned(live, instance)):
            skipped.append(ref)
            continue
        kube.delete(item["resource"], item["name"], namespace)
        deleted.append(ref)
    if kept:
        inventory.write(kube, {**record, "phase": "Removed", "objects": kept})
    else:
        kube.delete("configmaps", inventory.inventory_name(instance), namespace)
    return {
        "instance": instance,
        "namespace": namespace,
        "deleted": deleted,
        "kept": [f"{item['kind']}/{item['name']}" for item in kept],
        "skipped": skipped,
    }


def _split(resource: str) -> tuple[str, str]:
    """``plural.group`` -> (group, plural)."""
    plural, _, group = resource.partition(".")
    return group, plural
