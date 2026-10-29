"""Health of an instance, from its live objects and their pods.

An object belongs to the instance when its UID is the one recorded (objects llmd applied
also carry its labels; imported objects never do). A workload's pods are found through
the workload's own selector, never through an llmd label, so the same code judges stacks
llmd stood up and stacks deployed with ``oc apply`` or Helm.

A DisaggregatedSet is judged by its pods: every role must run ``replicas x group size``
pods, all Ready, none terminating. Its controller writes no status before lws v0.11.

An endpoint picker (EPP) with leader election passes its readiness check only while it
holds the lease, so one replica is Ready and the others are standbys that never are; its
Deployment is ready once one replica is.
"""

from __future__ import annotations

from typing import Any

#: Labels the DisaggregatedSet controller (lws) puts on every pod it creates.
DS_NAME_LABEL = "disaggregatedset.x-k8s.io/name"
DS_ROLE_LABEL = "disaggregatedset.x-k8s.io/role"
DS_REVISION_LABEL = "disaggregatedset.x-k8s.io/revision"
LWS_NAME_LABEL = "leaderworkerset.sigs.k8s.io/name"
GROUP_INDEX_LABEL = "leaderworkerset.sigs.k8s.io/group-index"

FAILING_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CrashLoopBackOff",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
}
PASSIVE_KINDS = {
    "ConfigMap",
    "Secret",
    "Service",
    "ServiceAccount",
    "Role",
    "RoleBinding",
    "NetworkPolicy",
    "PodDisruptionBudget",
    "PodMonitor",
    "ServiceMonitor",
    "InferenceObjective",
    "InferencePool",
    "HorizontalPodAutoscaler",
}


def labels(item: dict[str, Any]) -> dict[str, str]:
    return (item.get("metadata") or {}).get("labels") or {}


def pod_ready(pod: dict[str, Any]) -> bool:
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def pod_selector(item: dict[str, Any]) -> str:
    """The label selector of a workload's pods, as ``kubectl -l`` takes it ("" for other kinds)."""
    kind = item.get("kind")
    if kind == "DisaggregatedSet":
        return f"{DS_NAME_LABEL}={(item.get('metadata') or {}).get('name')}"
    if kind == "LeaderWorkerSet":
        return f"{LWS_NAME_LABEL}={(item.get('metadata') or {}).get('name')}"
    selector = (item.get("spec") or {}).get("selector") or {}
    match = selector.get("matchLabels") if isinstance(selector.get("matchLabels"), dict) else None
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet") and match:
        return ",".join(f"{key}={value}" for key, value in sorted(match.items()))
    return ""


def _conditions(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        c.get("type"): c for c in (item.get("status") or {}).get("conditions") or [] if isinstance(c, dict)
    }


#: The endpoint picker's flag for leader election (llm-d-router).
LEADER_ELECTION_FLAG = "--ha-enable-leader-election"


def leader_elected(item: dict[str, Any]) -> bool:
    """Whether a Deployment runs replicas of which only the lease holder is ever Ready."""
    template = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in template.get("containers") or []:
        for arg in [*(container.get("command") or []), *(container.get("args") or [])]:
            if arg in (LEADER_ELECTION_FLAG, f"{LEADER_ELECTION_FLAG}=true"):
                return True
    return False


def _generation_observed(item: dict[str, Any]) -> bool:
    generation = (item.get("metadata") or {}).get("generation")
    observed = (item.get("status") or {}).get("observedGeneration")
    return generation is None or observed is None or observed >= generation


def role_shape(item: dict[str, Any]) -> dict[str, dict[str, int]]:
    """``{role: {"replicas": groups, "size": pods per group}}`` from a DisaggregatedSet's spec."""
    shape = {}
    for index, role in enumerate((item.get("spec") or {}).get("roles") or []):
        spec = role.get("spec") or {}
        size = int((spec.get("leaderWorkerTemplate") or {}).get("size", 1) or 1)
        shape[str(role.get("name") or index)] = {"replicas": int(spec.get("replicas") or 0), "size": size}
    return shape


def role_counts(shape: dict[str, dict[str, int]], pods: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per role: the pods wanted (replicas x group size), present, Ready, and terminating."""
    counts = {}
    for role, wanted in shape.items():
        mine = [pod for pod in pods if labels(pod).get(DS_ROLE_LABEL) == role]
        draining = sum(1 for pod in mine if (pod.get("metadata") or {}).get("deletionTimestamp"))
        counts[role] = {
            "want": wanted["replicas"] * wanted["size"],
            "pods": len(mine),
            "ready": sum(1 for pod in mine if pod_ready(pod)),
            "draining": draining,
        }
    return counts


def settled(counts: dict[str, int]) -> bool:
    return counts["pods"] == counts["want"] == counts["ready"] and counts["draining"] == 0


def evaluate(item: dict[str, Any] | None, pods: list[dict[str, Any]]) -> tuple[str, str]:
    """(state, detail), state in ready | progressing | failed | present | missing. ``pods`` are the
    object's own pods (found with :func:`pod_selector`)."""
    if item is None:
        return "missing", "not found in the cluster"
    kind = item.get("kind")
    spec, status, conditions = item.get("spec") or {}, item.get("status") or {}, _conditions(item)
    if kind == "DisaggregatedSet":
        counts = role_counts(role_shape(item), pods)
        detail = "; ".join(
            f"{role} {value['ready']}/{value['want']} ready"
            + (f", {value['draining']} draining" if value["draining"] else "")
            for role, value in counts.items()
        )
        if not _generation_observed(item):
            return "progressing", "waiting for the controller to observe the latest generation"
        if counts and all(settled(value) for value in counts.values()):
            return "ready", detail
        return "progressing", detail or "no roles"
    if kind in ("Deployment", "StatefulSet"):
        desired = int(spec.get("replicas", 1) if spec.get("replicas") is not None else 1)
        ready = int(status.get("readyReplicas") or 0)
        if (conditions.get("Progressing") or {}).get("reason") == "ProgressDeadlineExceeded":
            return "failed", conditions["Progressing"].get("message", "progress deadline exceeded")
        # Pods refused at creation (an SCC, a quota) do not exist, so no pod reports the problem.
        refused = conditions.get("ReplicaFailure") or {}
        if refused.get("status") == "True":
            reason = refused.get("reason") or "ReplicaFailure"
            return "failed", f"{reason}: {(refused.get('message') or '')[:300]}"
        current = int(status.get("replicas") or 0)
        if desired == 0:
            return (
                ("progressing", f"scaling to 0: {current} pod(s) left")
                if current
                else ("ready", "scaled to 0")
            )
        updated = int(status.get("updatedReplicas") or 0)
        if _generation_observed(item) and ready >= desired and updated >= desired and current <= desired:
            return "ready", f"{ready}/{desired} ready"
        if kind == "Deployment" and leader_elected(item):
            rolled = _generation_observed(item) and updated >= desired and current <= desired
            if rolled and ready >= 1:
                return "ready", f"{ready}/{desired} ready (leader elected; the others stand by)"
        return "progressing", f"{ready}/{desired} ready"
    if kind == "Job":
        if (conditions.get("Complete") or {}).get("status") == "True":
            return "ready", "complete"
        if (conditions.get("Failed") or {}).get("status") == "True":
            return "failed", conditions["Failed"].get("message") or "failed"
        return "progressing", f"{status.get('active', 0)} active"
    if kind == "PersistentVolumeClaim":
        phase = status.get("phase", "Pending")
        return {"Bound": ("ready", "bound"), "Lost": ("failed", "lost")}.get(
            phase, ("progressing", "pending")
        )
    if kind == "Route":
        for ingress in status.get("ingress") or []:
            admitted = next((c for c in ingress.get("conditions") or [] if c.get("type") == "Admitted"), {})
            if admitted.get("status") == "True":
                return "ready", f"https://{ingress.get('host')}" if ingress.get("host") else "admitted"
            if admitted.get("status") == "False":
                return "failed", admitted.get("reason") or "not admitted"
        return "progressing", "waiting for a router to admit it"
    if kind in PASSIVE_KINDS:
        return "present", ""
    for name in ("Ready", "Available"):
        condition = conditions.get(name)
        if condition:
            state = "ready" if condition.get("status") == "True" else "progressing"
            return state, condition.get("reason") or condition.get("message") or name.lower()
    return "present", "no readiness conditions published"


def pod_problems(pods: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Why pods are not Ready: unschedulable, or a container waiting for a reason other than starting."""
    problems = []
    for pod in pods:
        metadata, status = pod.get("metadata") or {}, pod.get("status") or {}
        if metadata.get("deletionTimestamp") or pod_ready(pod) or status.get("phase") == "Succeeded":
            continue
        scheduled = next((c for c in status.get("conditions") or [] if c.get("type") == "PodScheduled"), {})
        if scheduled.get("status") == "False":
            problems.append(
                {
                    "pod": metadata.get("name", ""),
                    "reason": scheduled.get("reason") or "Unschedulable",
                    "message": (scheduled.get("message") or "")[:300],
                }
            )
            continue
        for container in [
            *(status.get("initContainerStatuses") or []),
            *(status.get("containerStatuses") or []),
        ]:
            waiting = (container.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") and waiting["reason"] not in ("ContainerCreating", "PodInitializing"):
                problem = {
                    "pod": metadata.get("name", ""),
                    "reason": waiting["reason"],
                    "message": (waiting.get("message") or "")[:300],
                    "container": container.get("name", ""),
                }
                ended = (container.get("lastState") or {}).get("terminated") or {}
                if ended.get("exitCode") is not None:
                    problem["exitCode"] = str(ended["exitCode"])
                problems.append(problem)
                break
    return problems


def overall(objects: list[dict[str, Any]], problems: list[dict[str, str]], *, partial: bool = False) -> str:
    """The instance's health. ``partial``: some objects or pods could not be read, so what looks
    ready is Unknown instead."""
    states = [item["state"] for item in objects]
    if not objects:
        return "Missing"
    if any(state in ("missing", "replaced") for state in states):
        return "Degraded"
    if any(state == "failed" for state in states) or any(p["reason"] in FAILING_REASONS for p in problems):
        return "Failed"
    if all(state in ("ready", "present", "unreadable") for state in states):
        return "Unknown" if partial else "Ready"
    return "Progressing"
