"""Where an in-place roll stands, read from the DisaggregatedSet's pods and LeaderWorkerSets.

The lws controller owns one LeaderWorkerSet per role and revision. During a roll a role has
the old revision's set (lowered step by step) and the new one's (raised step by step).
Everything here is read from labels the controller sets: ``disaggregatedset.x-k8s.io/
revision`` and ``role`` on pods and sets, ``leaderworkerset.sigs.k8s.io/group-index`` on pods.

A group is *serving* when all its pods are Ready and the pool label (when the stack has one,
``--pool-label``) says it is in the pool; drain tools take a group out of the pool by setting
that label to ``false`` before the controller removes it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..health import DS_NAME_LABEL, DS_REVISION_LABEL, DS_ROLE_LABEL, GROUP_INDEX_LABEL, labels, pod_ready
from ..kube import Kube

LWS_RESOURCE = "leaderworkersets.leaderworkerset.x-k8s.io"


@dataclass
class Group:
    revision: str
    index: int
    pods: int
    ready: int
    in_pool: bool
    terminating: bool

    @property
    def serving(self) -> bool:
        return self.ready == self.pods and self.in_pool and not self.terminating


@dataclass
class RoleProgress:
    role: str
    size: int
    replicas: int
    groups: list[Group] = field(default_factory=list)

    def by_revision(self) -> dict[str, list[Group]]:
        found: dict[str, list[Group]] = defaultdict(list)
        for group in self.groups:
            found[group.revision].append(group)
        return dict(found)

    def serving(self) -> int:
        return sum(1 for group in self.groups if group.serving)


@dataclass
class Progress:
    name: str
    #: The revision the controller rolls to: the newest one that has a LeaderWorkerSet.
    target: str
    revisions: list[str]
    roles: list[RoleProgress]

    @property
    def rolling(self) -> bool:
        return len(self.revisions) > 1

    def done(self, target: str | None = None) -> bool:
        """Every group of every role runs ``target`` (default: the newest revision), all serving."""
        target = target or self.target
        return all(
            len(role.groups) == role.replicas
            and all(group.revision == target and group.serving for group in role.groups)
            for role in self.roles
        )

    def describe(self) -> dict[str, Any]:
        return {
            "set": self.name,
            "target": self.target,
            "revisions": self.revisions,
            "rolling": self.rolling,
            "roles": {
                role.role: {
                    "replicas": role.replicas,
                    "serving": role.serving(),
                    "byRevision": {
                        revision: {
                            "groups": len(groups),
                            "serving": sum(1 for group in groups if group.serving),
                            "outOfPool": sum(1 for group in groups if not group.in_pool),
                        }
                        for revision, groups in role.by_revision().items()
                    },
                }
                for role in self.roles
            },
        }


def read_progress(kube: Kube, namespace: str, live: dict[str, Any], *, pool_label: str = "") -> Progress:
    """The roll's progress for the DisaggregatedSet ``live`` (its running object)."""
    name = live["metadata"]["name"]
    selector = f"{DS_NAME_LABEL}={name}"
    sets = kube.get_json(LWS_RESOURCE, namespace, selector=selector)
    pods = kube.get_json("pods", namespace, selector=selector)
    sets.sort(key=lambda item: (item["metadata"].get("creationTimestamp") or "", item["metadata"]["name"]))
    revisions = list(dict.fromkeys(filter(None, (labels(item).get(DS_REVISION_LABEL) for item in sets))))
    if not revisions:
        revisions = sorted({labels(pod).get(DS_REVISION_LABEL, "") for pod in pods} - {""})
    roles = []
    for index, role in enumerate((live.get("spec") or {}).get("roles") or []):
        spec = role.get("spec") or {}
        role_name = str(role.get("name") or index)
        progress = RoleProgress(
            role=role_name,
            size=int((spec.get("leaderWorkerTemplate") or {}).get("size") or 1),
            replicas=int(spec.get("replicas") or 0),
        )
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for pod in pods:
            pod_labels = labels(pod)
            if pod_labels.get(DS_ROLE_LABEL) != role_name or pod_labels.get(GROUP_INDEX_LABEL) is None:
                continue
            key = (pod_labels.get(DS_REVISION_LABEL, ""), int(pod_labels[GROUP_INDEX_LABEL]))
            grouped[key].append(pod)
        for (revision, group), members in sorted(grouped.items()):
            progress.groups.append(
                Group(
                    revision=revision,
                    index=group,
                    pods=progress.size,
                    ready=sum(1 for pod in members if pod_ready(pod)),
                    in_pool=not pool_label or all(labels(pod).get(pool_label) == "true" for pod in members),
                    terminating=any((pod.get("metadata") or {}).get("deletionTimestamp") for pod in members),
                )
            )
        roles.append(progress)
    return Progress(name=name, target=revisions[-1] if revisions else "", revisions=revisions, roles=roles)
