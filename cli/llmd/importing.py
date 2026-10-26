"""Record a stack another workflow deployed (``oc apply -k``, Helm) as an llmd instance.

An import changes nothing on the cluster except the instance's own record: it neither
labels nor applies the stack's objects, so a later ``oc apply`` by the team, or a Helm
upgrade, behaves exactly as before. Every object the stack renders must already exist;
the record keeps each one's UID, which is how status, rollouts and teardown recognise it.

``compare`` tells whether the live objects still match the stack's files: an import of an
overlay that does not match what runs would make the first rollout apply that difference.
"""

from __future__ import annotations

from typing import Any

from . import inventory
from .errors import ConflictError, NotFoundError
from .kube import Kube
from .plan import Plan
from .preflight import resources

#: Fields the API server or controllers own: never compared.
_SERVER_METADATA = {"uid", "resourceVersion", "generation", "creationTimestamp", "managedFields", "selfLink"}
_SERVER_ANNOTATIONS = (
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
)


def import_stack(kube: Kube, plan: Plan, *, manager: str, actor: str = "") -> dict[str, Any]:
    if inventory.read(kube, plan.namespace, plan.instance):
        raise ConflictError(
            f"instance {plan.instance} is already recorded in {plan.namespace}",
            hint=f"See `llmd status {plan.instance}`.",
        )
    objects, missing = [], []
    for document, resource in zip(plan.documents, resources(kube, plan), strict=True):
        live = kube.get(resource, document["metadata"]["name"], plan.namespace)
        if live is None:
            missing.append(f"{document['kind']}/{document['metadata']['name']}")
            continue
        objects.append(inventory.object_entry(document, resource, live["metadata"].get("uid", "")))
    if missing:
        raise NotFoundError(
            f"cannot import {plan.instance}: {len(missing)} object(s) of the stack are not in "
            f"{plan.namespace}",
            details=[{"message": item} for item in missing],
            hint="Point at the overlay that produced the running objects, or stand an empty namespace "
            "up with `llmd standup`.",
        )
    record = {
        "schema": inventory.SCHEMA,
        "instance": plan.instance,
        "namespace": plan.namespace,
        "management": {"mode": inventory.IMPORTED, "manager": manager},
        "phase": "Imported",
        "stack": {"id": plan.stack.id, "source": plan.stack.source.describe()},
        "planDigest": plan.digest,
        "objects": objects,
        "createdAt": inventory.now(),
        "updatedBy": actor,
    }
    inventory.write(kube, record)
    return {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "imported": len(objects),
        "manager": manager,
    }


def drift(desired: Any, live: Any, path: str = "") -> list[str]:
    """Paths where ``live`` differs from ``desired``, for the fields ``desired`` sets. Fields only the
    live object has (defaults, status) are not drift."""
    if isinstance(desired, dict) and isinstance(live, dict):
        found = []
        for key, value in desired.items():
            found += drift(value, live.get(key), f"{path}.{key}" if path else str(key))
        return found
    if isinstance(desired, list) and isinstance(live, list) and len(desired) == len(live):
        found = []
        for index, (want, have) in enumerate(zip(desired, live, strict=True)):
            found += drift(want, have, f"{path}[{index}]")
        return found
    if desired != live and not (desired in (None, "", {}, []) and live in (None, "", {}, [])):
        return [path]
    return []


def comparable(document: dict[str, Any]) -> dict[str, Any]:
    """An object without what the server owns, for comparing a render with a live object."""
    metadata = dict(document.get("metadata") or {})
    for key in _SERVER_METADATA:
        metadata.pop(key, None)
    annotations = {
        key: value
        for key, value in (metadata.get("annotations") or {}).items()
        if key not in _SERVER_ANNOTATIONS
    }
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)
    return {key: value for key, value in {**document, "metadata": metadata}.items() if key != "status"}


def compare(kube: Kube, plan: Plan) -> list[dict[str, Any]]:
    """For each object of the stack: whether the live object matches the files, and where not."""
    report = []
    for document, resource in zip(plan.documents, resources(kube, plan), strict=True):
        live = kube.get(resource, document["metadata"]["name"], plan.namespace)
        ref = f"{document['kind']}/{document['metadata']['name']}"
        if live is None:
            report.append({"object": ref, "state": "missing", "differences": []})
            continue
        differences = drift(comparable(document), comparable(live))
        report.append(
            {"object": ref, "state": "drifted" if differences else "matches", "differences": differences}
        )
    return report
