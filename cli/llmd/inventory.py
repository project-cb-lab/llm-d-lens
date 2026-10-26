"""The in-cluster record of an instance: one ConfigMap per instance, in the instance's namespace.

The cluster, not a laptop, is the source of truth for what an instance owns, so status,
rollouts and teardown work from any machine and after a crash. The record holds object
identities and UIDs, and it is written before the first object is applied, so an
interrupted standup can still be torn down. During a rollout it also holds the bodies of
the objects the rollout changes, as they were (``rollout.previous``), which is what
``rollouts abort`` re-applies; Secrets are never among them, since a rollout never applies
one the instance does not render. A DisaggregatedSet body is tens of kilobytes, well inside
a ConfigMap's 1 MiB.

``management`` says how the instance's objects got there:

* ``llmd``: llmd applied them (server-side apply, labelled with the instance);
* ``imported``: another workflow deployed them (``oc apply``, Helm); llmd recorded them by
  UID and never labels them. ``manager`` names that workflow.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .errors import ConflictError, NotFoundError
from .kube import Kube
from .model import INSTANCE_LABEL, MANAGED_BY_LABEL, MANAGED_BY_VALUE, validate_instance_name

SCHEMA = "lens.llm-d.ai/inventory/v1"
PREFIX = "lens-instance-"
ROLE_LABEL = "lens.llm-d.ai/role"
ROLE_VALUE = "inventory"
DATA_KEY = "inventory.json"
MANAGED, IMPORTED = "llmd", "imported"


def inventory_name(instance: str) -> str:
    return f"{PREFIX}{instance}"


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def configmap(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": inventory_name(record["instance"]),
            "namespace": record["namespace"],
            "labels": {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                INSTANCE_LABEL: record["instance"],
                ROLE_LABEL: ROLE_VALUE,
            },
        },
        "data": {DATA_KEY: json.dumps(record, indent=2, sort_keys=True)},
    }


def parse(item: dict[str, Any], *, namespace: str) -> dict[str, Any]:
    """The record in an inventory ConfigMap; refuses one whose labels, name or schema do not match."""
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    try:
        record = json.loads((item.get("data") or {}).get(DATA_KEY) or "")
    except ValueError as exc:
        raise ConflictError(f"inventory {metadata.get('name')} is not valid JSON") from exc
    instance = record.get("instance") if isinstance(record, dict) else None
    valid = (
        isinstance(record, dict)
        and record.get("schema") == SCHEMA
        and labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
        and labels.get(INSTANCE_LABEL) == instance
        and metadata.get("name") == inventory_name(str(instance))
        and record.get("namespace") == namespace == metadata.get("namespace")
        and isinstance(record.get("objects"), list)
    )
    if not valid:
        raise ConflictError(
            f"inventory {metadata.get('name')} does not match its labels or schema; refusing to use it",
            hint="Inspect it with kubectl; it may have been edited or created by another tool.",
        )
    return record


def read(kube: Kube, namespace: str, instance: str) -> dict[str, Any] | None:
    validate_instance_name(instance)
    item = kube.get("configmaps", inventory_name(instance), namespace)
    return parse(item, namespace=namespace) if item else None


def require(kube: Kube, namespace: str, instance: str) -> dict[str, Any]:
    record = read(kube, namespace, instance)
    if record is None:
        raise NotFoundError(
            f"instance {instance} not found in {namespace}",
            hint=f"List instances with `llmd list -n {namespace}`.",
        )
    return record


def write(kube: Kube, record: dict[str, Any]) -> None:
    kube.apply([configmap({**record, "updatedAt": now()})], record["namespace"])


def list_records(kube: Kube, namespace: str) -> list[dict[str, Any]]:
    selector = f"{ROLE_LABEL}={ROLE_VALUE},{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
    records = []
    for item in kube.get_json("configmaps", namespace, selector=selector):
        try:
            records.append(parse(item, namespace=namespace))
        except ConflictError as error:
            records.append(
                {
                    "instance": (item.get("metadata") or {}).get("name"),
                    "phase": "Invalid",
                    "error": error.message,
                }
            )
    return sorted(records, key=lambda record: str(record.get("instance")))


def management(record: dict[str, Any]) -> str:
    return (record.get("management") or {}).get("mode") or MANAGED


def object_entry(document: dict[str, Any], resource: str, uid: str = "") -> dict[str, str]:
    entry = {
        "apiVersion": document["apiVersion"],
        "kind": document["kind"],
        "name": document["metadata"]["name"],
        "resource": resource,
    }
    if uid:
        entry["uid"] = uid
    return entry
