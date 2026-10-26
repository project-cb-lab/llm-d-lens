"""``rollouts plan``: what moving an instance to a source ref would do, read-only.

The plan renders the stack at the new ref, compares it with the objects the instance runs,
and refuses what is not one safe in-place rollout:

* the instance is not settled: a roll is already running, or a group is not serving;
* more than one DisaggregatedSet would roll at once;
* a role would be added or removed (that is not an in-place roll);
* ``--floor`` says a role must keep N groups serving and a step would go below it;
* the stack renders an object the namespace policy refuses (the same checks as a standup).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import inventory
from ..errors import ConflictError, PolicyError
from ..kube import Kube
from ..model import Finding, object_ref
from ..plan import Plan
from ..preflight import resources
from .change import Change, compare_objects
from .progress import Progress, read_progress


@dataclass
class RolloutPlan:
    plan: Plan
    record: dict[str, Any]
    change: Change
    #: The rolling set's progress before the rollout (None when nothing rolls).
    before: Progress | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def digest(self) -> str:
        return self.plan.digest

    def summary(self) -> dict[str, Any]:
        roll = self.change.roll
        return {
            "instance": self.plan.instance,
            "namespace": self.plan.namespace,
            "management": self.record.get("management") or {"mode": inventory.MANAGED},
            "stack": self.plan.stack.id,
            "from": (self.record.get("stack") or {}).get("source", {}),
            "to": self.plan.stack.source.describe(),
            "objects": {ref: state for ref, state in self.change.objects.items() if state != "same"},
            "unchanged": sum(1 for state in self.change.objects.values() if state == "same"),
            "leftAlone": self.change.left_alone,
            "sets": [
                {
                    "name": item.name,
                    "change": item.kind,
                    "steps": item.steps(),
                    "servingDuringAStep": item.serving_during() if item.kind == "roll" else {},
                    "roles": [
                        {
                            "role": role.name,
                            "replicas": list(role.replicas),
                            "podsPerGroup": role.size,
                            "maxUnavailable": role.max_unavailable,
                            "maxSurge": role.max_surge,
                        }
                        for role in item.roles
                    ],
                    "differences": item.differences[:40],
                }
                for item in self.change.sets
            ],
            "revision": self.before.target if (roll and self.before) else "",
            "findings": [finding.as_dict() for finding in self.findings],
            "digest": self.digest,
            "blocking": len(self.blocking),
        }


def plan_rollout(kube: Kube, plan: Plan, *, floors: dict[str, int], pool_label: str = "") -> RolloutPlan:
    record = inventory.require(kube, plan.namespace, plan.instance)
    phase = (record.get("rollout") or {}).get("phase")
    if phase in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(
            f"instance {plan.instance} already has a rollout in progress ({phase})",
            hint=f"Follow it with `llmd rollouts status {plan.instance}`; finish or abort it first.",
        )
    if record["stack"]["id"] != plan.stack.id:
        raise PolicyError(
            f"instance {plan.instance} runs stack {record['stack']['id']}, not {plan.stack.id}",
            hint="A rollout moves an instance to another ref of the same stack.",
        )
    recorded = [f"{item['kind']}/{item['name']}" for item in record["objects"]]
    live = {}
    for document, resource in zip(plan.documents, resources(kube, plan), strict=True):
        live[object_ref(document)] = kube.get(resource, document["metadata"]["name"], plan.namespace)
    change = compare_objects(live, plan.documents, recorded)
    findings = [finding for finding in plan.findings if finding.code not in _OBSERVED]
    rolling = [item for item in change.sets if item.kind == "roll"]
    if len(rolling) > 1:
        findings.append(
            Finding(
                "error",
                "several-rolls",
                f"{', '.join(item.name for item in rolling)} would all roll at once",
                hint="Roll one DisaggregatedSet per rollout.",
            )
        )
    before = None
    for item in change.sets:
        current = live[f"DisaggregatedSet/{item.name}"]
        progress = read_progress(kube, plan.namespace, current, pool_label=pool_label)
        if item.kind == "roll":
            before = progress
        findings.extend(_settled(progress))
        if item.kind != "roll":
            continue
        roles_before = {str(role.get("name")) for role in current["spec"].get("roles") or []}
        roles_after = {role.name for role in item.roles}
        if roles_before != roles_after:
            findings.append(
                Finding(
                    "error",
                    "roles-changed",
                    f"{item.name}: roles change from {sorted(roles_before)} to {sorted(roles_after)}",
                    hint="Adding or removing a role is not an in-place roll.",
                )
            )
        for role, serving in item.serving_during().items():
            floor = floors.get(role)
            if floor is not None and serving < floor:
                findings.append(
                    Finding(
                        "error",
                        "below-floor",
                        f"{item.name}/{role}: each step keeps {serving} group(s) serving, below the "
                        f"floor of {floor}",
                        hint="Lower the role's maxUnavailable in the stack, or the floor.",
                    )
                )
    if not any(state != "same" for state in change.objects.values()):
        findings.append(Finding("info", "nothing-to-do", "the instance already runs what this ref renders"))
    return RolloutPlan(plan=plan, record=record, change=change, before=before, findings=findings)


#: Plan findings about the objects the instance already runs: an imported stack was checked
#: (and acknowledged) when it was deployed, so they describe it rather than block a rollout.
_OBSERVED = {"instance-imported", "object-exists"}


def _settled(progress: Progress) -> list[Finding]:
    """A rollout starts from a settled set: one revision, every group serving."""
    if progress.rolling:
        return [
            Finding(
                "error",
                "roll-in-progress",
                f"{progress.name} is rolling ({' -> '.join(progress.revisions)})",
                hint="Follow it with `llmd rollouts status`, or wait for it to finish.",
            )
        ]
    found = []
    for role in progress.roles:
        if role.serving() < role.replicas:
            found.append(
                Finding(
                    "error",
                    "not-settled",
                    f"{progress.name}/{role.role}: {role.serving()} of {role.replicas} group(s) are serving",
                    hint="Start a rollout once every group is Ready and in the pool.",
                )
            )
    return found
