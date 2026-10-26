"""What a rollout changes: the running objects compared with what a source ref renders.

For each DisaggregatedSet the change is one of

* ``roll``: a role's ``leaderWorkerTemplate`` changed. lws hashes each role's name and
  ``leaderWorkerTemplate`` into the revision, so every group of every role is replaced;
* ``scale``: only ``replicas`` changed: groups are added or removed, nothing rolls;
* ``strategy``: only ``rolloutStrategy`` (or another field outside the revision) changed;
* ``none``.

and every other object either stays, changes in place, or is new; an object the running
instance has and the new render does not is never deleted by a rollout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..importing import comparable, drift
from ..model import object_ref

#: The parts of a role that lws hashes into the revision (``ComputeRevision``).
REVISION_FIELDS = ("leaderWorkerTemplate",)


@dataclass
class RoleChange:
    name: str
    replicas: tuple[int, int]  # before, after
    size: int  # pods per group
    max_unavailable: int  # groups replaced per step
    max_surge: int
    template_changed: bool


@dataclass
class SetChange:
    name: str
    kind: str  # roll | scale | strategy | none
    roles: list[RoleChange]
    #: Field paths that differ, for the plan's output.
    differences: list[str] = field(default_factory=list)

    def steps(self) -> int:
        """Steps of the controller's roll: the slowest role sets it (roles step together)."""
        if self.kind != "roll":
            return 0
        return max(
            (
                math.ceil(role.replicas[1] / max(role.max_unavailable, 1))
                for role in self.roles
                if role.replicas[1]
            ),
            default=0,
        )

    def serving_during(self) -> dict[str, int]:
        """Groups of each role that keep serving during a step (``maxSurge: 0``): ``replicas - N``."""
        return {
            role.name: max(role.replicas[0] - (0 if role.max_surge else role.max_unavailable), 0)
            for role in self.roles
        }


def _roles(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(role.get("name") or index): role.get("spec") or {}
        for index, role in enumerate((document.get("spec") or {}).get("roles") or [])
    }


def _int_or_percent(value: Any, of: int, *, round_up: bool) -> int:
    if isinstance(value, str) and value.endswith("%"):
        share = of * int(value[:-1]) / 100
        return math.ceil(share) if round_up else math.floor(share)
    return int(value or 0)


def set_change(live: dict[str, Any], desired: dict[str, Any]) -> SetChange:
    """How a DisaggregatedSet changes from ``live`` to ``desired``."""
    before, after = _roles(live), _roles(desired)
    roles = []
    rolls = scales = False
    for name, spec in after.items():
        old = before.get(name) or {}
        strategy = ((spec.get("rolloutStrategy") or {}).get("rollingUpdateConfiguration")) or {}
        replicas = int(spec.get("replicas") or 0)
        changed = any(
            drift(comparable_part(spec.get(key)), comparable_part(old.get(key))) for key in REVISION_FIELDS
        )
        roles.append(
            RoleChange(
                name=name,
                replicas=(int(old.get("replicas") or 0), replicas),
                size=int((spec.get("leaderWorkerTemplate") or {}).get("size") or 1),
                # lws defaults: maxUnavailable 1, maxSurge 0 (as for LeaderWorkerSet).
                max_unavailable=max(
                    _int_or_percent(strategy.get("maxUnavailable", 1), replicas, round_up=False), 1
                ),
                max_surge=_int_or_percent(strategy.get("maxSurge", 0), replicas, round_up=True),
                template_changed=changed or name not in before,
            )
        )
        rolls = rolls or changed or name not in before
        scales = scales or roles[-1].replicas[0] != roles[-1].replicas[1]
    removed_roles = set(before) - set(after)
    differences = drift(comparable(desired).get("spec") or {}, comparable(live).get("spec") or {}, "spec")
    if rolls or removed_roles:
        kind = "roll"
    elif scales:
        kind = "scale"
    elif differences:
        kind = "strategy"
    else:
        kind = "none"
    return SetChange(name=desired["metadata"]["name"], kind=kind, roles=roles, differences=differences)


def comparable_part(value: Any) -> Any:
    """A part of a spec as compared: ``None`` and empty values are the same."""
    return value if value not in (None, {}, []) else None


@dataclass
class Change:
    """The whole rollout: one entry per object of the new render."""

    sets: list[SetChange]
    #: object ref -> "same" | "changed" | "new"
    objects: dict[str, str]
    #: Objects the instance runs that the new render no longer has; a rollout leaves them alone.
    left_alone: list[str]

    @property
    def roll(self) -> SetChange | None:
        return next((item for item in self.sets if item.kind == "roll"), None)

    def changed_documents(self, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [doc for doc in documents if self.objects.get(object_ref(doc)) in ("changed", "new")]


def compare_objects(
    live: dict[str, dict[str, Any] | None], documents: list[dict[str, Any]], recorded: list[str]
) -> Change:
    """``live`` maps each object ref of ``documents`` to its running object (None when it is new)."""
    objects, sets = {}, []
    for document in documents:
        ref = object_ref(document)
        current = live.get(ref)
        if current is None:
            objects[ref] = "new"
            continue
        if document["kind"] == "DisaggregatedSet":
            item = set_change(current, document)
            sets.append(item)
            objects[ref] = "same" if item.kind == "none" else "changed"
            continue
        objects[ref] = "changed" if drift(comparable(document), comparable(current)) else "same"
    left_alone = sorted(set(recorded) - set(objects))
    return Change(sets=sets, objects=objects, left_alone=left_alone)
