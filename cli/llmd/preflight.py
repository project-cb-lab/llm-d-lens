"""Read-only checks of a plan against the cluster. Nothing here changes the cluster.

* the namespace exists and the cluster serves every API the stack uses;
* the user may get, list, create, patch and delete what the plan applies;
* the prerequisites (Secrets, claims, ServiceAccounts, network attachments) are present;
* every object either does not exist yet or belongs to this instance: llmd never overwrites
  an object it did not create (``import`` records such objects instead);
* the server accepts the objects (a server-side dry run).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from . import inventory
from .errors import CommandError
from .kube import Kube
from .model import INSTANCE_LABEL, MANAGED_BY_LABEL, MANAGED_BY_VALUE, Finding, api_group, object_ref
from .plan import Plan

#: Prerequisite kind -> (apiVersion, kubectl resource).
PREREQUISITES = {
    "Secret": ("v1", "secrets"),
    "ConfigMap": ("v1", "configmaps"),
    "ServiceAccount": ("v1", "serviceaccounts"),
    "PersistentVolumeClaim": ("v1", "persistentvolumeclaims"),
    "Role": ("rbac.authorization.k8s.io/v1", "roles.rbac.authorization.k8s.io"),
    "NetworkAttachmentDefinition": ("k8s.cni.cncf.io/v1", "network-attachment-definitions.k8s.cni.cncf.io"),
}


def preflight(kube: Kube, plan: Plan, *, existing: str = "refuse") -> None:
    """Check ``plan`` against the cluster and add findings to it.

    ``existing`` says what an object that is already there but not this instance's means:
    ``refuse`` (standup: an error) or ``import`` (the objects must all be there)."""
    findings = plan.findings
    plan.cluster["context"] = kube.current_context()
    plan.cluster["user"] = kube.whoami()
    state = kube.namespace_state(plan.namespace)
    plan.cluster["namespace"] = state
    if state == "missing":
        findings.append(
            Finding(
                "error",
                "namespace-missing",
                f"namespace {plan.namespace} does not exist",
                hint="llmd never creates namespaces; ask an administrator to create it.",
            )
        )
        return
    if not _check_apis(kube, plan):
        return
    if existing == "import":
        _observe_only(plan)
    _check_permissions(kube, plan, write=existing != "import")
    _check_prerequisites(kube, plan)
    _check_existing(kube, plan, existing)
    if plan.blocking or existing == "import":
        plan.cluster["dryRun"] = "skipped"
        return
    try:
        kube.apply(plan.documents, plan.namespace, dry_run=True)
        plan.cluster["dryRun"] = "passed"
    except CommandError as error:
        plan.cluster["dryRun"] = "failed"
        findings.append(Finding("error", "dry-run-failed", error.message, hint=error.hint or ""))


def _check_apis(kube: Kube, plan: Plan) -> bool:
    missing = sorted(
        {
            f"{doc['apiVersion']} {doc['kind']}"
            for doc in plan.documents
            if kube.resource(doc["apiVersion"], doc["kind"]) is None
        }
    )
    for item in missing:
        plan.findings.append(
            Finding(
                "error",
                "api-missing",
                f"the cluster does not serve {item}",
                hint="An administrator installs the CRD or operator that serves it.",
            )
        )
    return not missing


def resources(kube: Kube, plan: Plan) -> list[str]:
    """The kubectl resource name of each planned object, in plan order."""
    return [kube.resource_name(doc) for doc in plan.documents]


#: Findings that guard what an apply would create. An import applies nothing, so for an import
#: they describe the running stack and do not block.
APPLY_ONLY = {"privileged", "host-path", "host-namespace", "scc-binding", "cluster-scoped"}


def _observe_only(plan: Plan) -> None:
    plan.policy.findings[:] = [
        replace(finding, severity="info") if finding.code in APPLY_ONLY else finding
        for finding in plan.findings
    ]


def _check_permissions(kube: Kube, plan: Plan, *, write: bool = True) -> None:
    """What the user must be allowed: for a standup, to change every kind the plan applies; for an
    import, only to read them and to write the instance's record (a ConfigMap)."""
    names = sorted({*resources(kube, plan), "configmaps"})
    if write:
        checks = [(verb, name, "error") for name in names for verb in ("get", "list", "create", "patch")]
        checks += [("delete", name, "warning") for name in names]
    else:
        checks = [(verb, name, "error") for name in names for verb in ("get", "list")]
        checks += [(verb, "configmaps", "error") for verb in ("create", "patch")]
    checks += [("get", "pods", "error"), ("list", "pods", "error")]
    reviews = [
        (verb, name.partition(".")[2], name.partition(".")[0], plan.namespace) for verb, name, _ in checks
    ]
    denied: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (verb, name, severity), allowed in zip(checks, kube.access_reviews(reviews), strict=True):
        if allowed is False:
            denied[(severity, verb)].append(name)
    plan.cluster["permissions"] = "ok" if not denied else {verb: items for (_, verb), items in denied.items()}
    for (severity, verb), items in sorted(denied.items()):
        listed = ", ".join(sorted(items))
        if severity == "error":
            message = f"you cannot {verb} {listed} in {plan.namespace}"
            hint = "Ask a namespace administrator for a Role that allows it."
        else:
            message = f"you cannot delete {listed} in {plan.namespace}; a teardown would be incomplete"
            hint = ""
        plan.findings.append(
            Finding(severity, f"permission-{verb if severity == 'warning' else 'denied'}", message, hint=hint)
        )


def _check_prerequisites(kube: Kube, plan: Plan) -> None:
    for item in plan.policy.prerequisites:
        api_version, resource = PREREQUISITES.get(item["kind"], ("", ""))
        if not resource or kube.resource(api_version, item["kind"]) is None:
            item["state"] = "unknown"
            continue
        try:
            item["state"] = "present" if kube.get(resource, item["name"], plan.namespace) else "missing"
        except CommandError:
            item["state"] = "unverified"
            continue
        if item["state"] == "missing":
            plan.findings.append(
                Finding(
                    "error",
                    "prerequisite-missing",
                    f"{item['kind']}/{item['name']} must already exist in {plan.namespace}",
                    object=f"{item['kind']}/{item['name']}",
                    hint=f"Used by {', '.join(item['usedBy'])}. Create it first, or ask the namespace's "
                    "administrator.",
                )
            )


def _check_existing(kube: Kube, plan: Plan, existing: str) -> None:
    """Who owns each planned object today."""
    record = inventory.read(kube, plan.namespace, plan.instance)
    if record and inventory.management(record) == inventory.IMPORTED and existing != "import":
        plan.findings.append(
            Finding(
                "error",
                "instance-imported",
                f"instance {plan.instance} was imported from another deployment workflow",
                hint="Change it with `llmd rollouts`, or stop managing it with "
                f"`llmd teardown {plan.instance} --forget`.",
            )
        )
    owned = {(item["kind"], item["name"]) for item in (record or {}).get("objects") or []}
    counts = {"create": 0, "update": 0, "foreign": 0}
    found: list[dict[str, Any]] = []
    for document, resource in zip(plan.documents, resources(kube, plan), strict=True):
        live = kube.get(resource, document["metadata"]["name"], plan.namespace)
        if live is None:
            counts["create"] += 1
            continue
        labels = (live.get("metadata") or {}).get("labels") or {}
        ours = (
            labels.get(INSTANCE_LABEL) == plan.instance and labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
        )
        if ours or (document["kind"], document["metadata"]["name"]) in owned:
            counts["update"] += 1
            continue
        counts["foreign"] += 1
        found.append({"object": object_ref(document), "group": api_group(document["apiVersion"])})
        if existing == "refuse":
            owner = (
                f"instance {labels[INSTANCE_LABEL]}" if labels.get(INSTANCE_LABEL) else "another tool or user"
            )
            plan.findings.append(
                Finding(
                    "error",
                    "object-exists",
                    f"{object_ref(document)} already exists and belongs to {owner}",
                    object=object_ref(document),
                    hint="llmd never overwrites objects it did not create. If this stack was deployed with "
                    "`oc apply` or Helm, record it with `llmd import` instead.",
                )
            )
    plan.cluster["objects"] = counts
