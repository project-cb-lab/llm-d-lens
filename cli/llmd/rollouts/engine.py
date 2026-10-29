"""Start, follow, pause, resume, abort and finish an in-place rollout.

The rollout's state lives in the instance's record (``rollout``), so ``status``, ``pause``
and ``abort`` work from any machine; only the drainer processes are local to the machine
that started them (``pause``/``resume`` there, or start new drainers with ``resume``).

``start`` applies only the objects that change. An imported stack's objects are applied
the way its team applies them (client-side ``kubectl apply``), so their ``oc apply``
keeps working; objects llmd stood up use server-side apply as at standup.

``abort`` re-applies the DisaggregatedSet as it was before the rollout: the controller has
no pause and does not read ``partition``, so the old template is the only way back. It is
itself a roll, and the drainers run for it the same way.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

from .. import inventory, yamlio
from ..errors import ConflictError, NotReadyError, PolicyError, UsageError
from ..importing import comparable
from ..kube import Kube
from ..model import object_ref
from ..preflight import resources
from .drainers import Drainer, Drainers, command
from .plan import RolloutPlan
from .progress import Progress, read_progress

DS_RESOURCE = "disaggregatedsets.disaggregatedset.x-k8s.io"


def _record(kube: Kube, namespace: str, instance: str) -> dict[str, Any]:
    return inventory.require(kube, namespace, instance)


def _rollout(record: dict[str, Any]) -> dict[str, Any]:
    rollout = record.get("rollout") or {}
    if rollout.get("phase") not in ("Rolling", "Paused", "Aborting"):
        raise UsageError(
            f"instance {record['instance']} has no rollout in progress",
            hint="Start one with `llmd rollouts start`.",
        )
    return rollout


def drainer_plan(rollout: RolloutPlan, template: str, directory: Path) -> list[Drainer]:
    roll = rollout.change.roll
    if roll is None or not template:
        return []
    drainers = []
    for role in roll.roles:
        values = {
            "role": role.name,
            "source": str(rollout.plan.stack.source.root),
            "namespace": rollout.plan.namespace,
            "set": roll.name,
            "old_revision": rollout.before.target if rollout.before else "",
            "max_unavailable": str(role.max_unavailable),
        }
        log = directory / f"drain-{role.name}.log"
        drainers.append(
            Drainer(role.name, command(template, values), log, str(rollout.plan.stack.source.root))
        )
    return drainers


def start(
    kube: Kube,
    rollout: RolloutPlan,
    *,
    drain: str,
    state: Path,
    expected_digest: str = "",
    env: dict[str, str] | None = None,
    actor: str = "",
) -> dict[str, Any]:
    plan, record = rollout.plan, rollout.record
    if rollout.blocking:
        raise PolicyError(
            f"the rollout has {len(rollout.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in rollout.blocking],
            hint="Run `llmd rollouts plan` to see them.",
        )
    if expected_digest and not yamlio.digest_matches(expected_digest, rollout.digest):
        raise ConflictError(
            "the rollout changed since it was reviewed", hint="Review `llmd rollouts plan` again."
        )
    if (record.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(f"instance {plan.instance} already has a rollout in progress")
    roll = rollout.change.roll
    if roll and not drain:
        raise UsageError(
            f"{roll.name} rolls: every group is replaced, so its roles need a drain command",
            hint="Pass --drain 'COMMAND {role} ...' (the stack's drain tool), or --no-drain to roll "
            "undrained.",
        )
    changed = rollout.change.changed_documents(plan.documents)
    if not changed:
        raise UsageError("nothing to roll out: the instance already runs what this ref renders")
    secrets = [object_ref(doc) for doc in changed if doc["kind"] == "Secret"]
    if secrets:
        # The record keeps what a rollout changes, as it was, for abort; never a Secret's data.
        raise PolicyError(
            f"the rollout would change {', '.join(secrets)}",
            hint="Update Secrets outside a rollout; llmd never records their data.",
        )
    names = dict(zip((object_ref(doc) for doc in plan.documents), resources(kube, plan), strict=True))
    previous = []
    for document in changed:
        live = kube.get(names[object_ref(document)], document["metadata"]["name"], plan.namespace)
        if live is not None:
            previous.append({"resource": names[object_ref(document)], "document": comparable(live)})
    directory = state / "rollouts" / plan.namespace / plan.instance
    planned_drainers = drainer_plan(rollout, drain, directory) if drain != "-" else []
    record = {
        **record,
        "rollout": {
            "phase": "Rolling",
            "startedAt": inventory.now(),
            "startedBy": actor,
            "from": (record.get("stack") or {}).get("source"),
            "to": plan.stack.source.describe(),
            "digest": rollout.digest,
            "set": roll.name if roll else "",
            "fromRevision": rollout.before.target if (roll and rollout.before) else "",
            "objects": [object_ref(doc) for doc in changed],
            "previous": previous,
            "drain": drain if drain != "-" else "",
            "steps": [],
        },
    }
    inventory.write(kube, record)
    started = Drainers(directory).start(planned_drainers, env=env) if planned_drainers else []
    imported = inventory.management(record) == inventory.IMPORTED
    kube.apply(changed, plan.namespace, server_side=not imported)
    record = _record(kube, plan.namespace, plan.instance)
    record["stack"] = {"id": plan.stack.id, "source": plan.stack.source.describe()}
    record["planDigest"] = plan.digest
    new = [
        inventory.object_entry(doc, names[object_ref(doc)])
        for doc in changed
        if rollout.change.objects[object_ref(doc)] == "new"
    ]
    for entry in new:
        entry["uid"] = (
            (kube.get(entry["resource"], entry["name"], plan.namespace) or {})
            .get("metadata", {})
            .get("uid", "")
        )
    record["objects"] = [*record["objects"], *new]
    if not roll:
        record["rollout"] = {**record["rollout"], "phase": "Finished", "finishedAt": inventory.now()}
    inventory.write(kube, record)
    return {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "applied": [object_ref(doc) for doc in changed],
        "rolls": roll.name if roll else "",
        "drainers": [item.describe() for item in started],
        "phase": record["rollout"]["phase"],
    }


def progress_of(kube: Kube, record: dict[str, Any], *, pool_label: str) -> Progress | None:
    name = (record.get("rollout") or {}).get("set")
    if not name:
        return None
    live = kube.require(DS_RESOURCE, name, record["namespace"])
    progress = read_progress(kube, record["namespace"], live, pool_label=pool_label)
    rollout = record.get("rollout") or {}
    if rollout.get("phase") == "Aborting" and rollout.get("fromRevision"):
        # An abort rolls back to the revision the rollout started from, though its sets are older.
        progress.target = rollout["fromRevision"]
    return progress


def status(kube: Kube, namespace: str, instance: str, *, state: Path, pool_label: str) -> dict[str, Any]:
    record = _record(kube, namespace, instance)
    rollout = record.get("rollout") or {}
    progress = progress_of(kube, record, pool_label=pool_label) if rollout.get("set") else None
    drainers = Drainers(state / "rollouts" / namespace / instance)
    return {
        "instance": instance,
        "namespace": namespace,
        "phase": rollout.get("phase") or "None",
        "set": rollout.get("set", ""),
        "from": (rollout.get("from") or {}).get("revision", ""),
        "to": (rollout.get("to") or {}).get("revision", ""),
        "fromRevision": rollout.get("fromRevision", ""),
        "progress": progress.describe() if progress else None,
        "done": _done(rollout, progress),
        "drainers": {
            "running": [item.describe() for item in drainers.running()],
            "exited": [item.describe() for item in drainers.exited()],
        },
        "startedAt": rollout.get("startedAt", ""),
    }


def _done(rollout: dict[str, Any], progress: Progress | None) -> bool:
    """Rolled forward: every group serves a revision other than the one the rollout started from.
    Aborted: every group serves that revision again."""
    if progress is None or progress.rolling or not progress.done():
        return False
    back = progress.target == rollout.get("fromRevision")
    return back if rollout.get("phase") == "Aborting" else not back


def _set_phase(kube: Kube, record: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
    record = {**record, "rollout": {**record["rollout"], "phase": phase, **extra}}
    inventory.write(kube, record)
    return record


def pause(kube: Kube, namespace: str, instance: str, *, state: Path) -> dict[str, Any]:
    """Stop the drainers: the stack's guards then hold the controller where it stands."""
    record = _record(kube, namespace, instance)
    _rollout(record)
    stopped = Drainers(state / "rollouts" / namespace / instance).stop()
    _set_phase(kube, record, "Paused", pausedAt=inventory.now())
    return {"instance": instance, "phase": "Paused", "stopped": [item.role for item in stopped]}


def resume(kube: Kube, namespace: str, instance: str, *, state: Path, env=None) -> dict[str, Any]:
    """Start the drainers again (on this machine) and mark the rollout Rolling."""
    record = _record(kube, namespace, instance)
    rollout = _rollout(record)
    directory = state / "rollouts" / namespace / instance
    drainers = Drainers(directory)
    if drainers.running():
        raise ConflictError("the drainers already run", hint="See `llmd rollouts status`.")
    restart = [Drainer(item.role, item.argv, item.log, item.cwd) for item in drainers.load()]
    if not restart and rollout.get("drain"):
        raise UsageError(
            "this machine did not start the rollout's drainers",
            hint="Resume on the machine that ran `llmd rollouts start`, or run the stack's drain "
            "command there.",
        )
    started = drainers.start(restart, env=env) if restart else []
    _set_phase(kube, record, "Aborting" if rollout["phase"] == "Aborting" else "Rolling")
    return {"instance": instance, "phase": "Rolling", "drainers": [item.describe() for item in started]}


def abort(kube: Kube, namespace: str, instance: str, *, server_side: bool) -> dict[str, Any]:
    """Re-apply the objects as they were before the rollout. The controller rolls back the same
    way it rolled forward, so the drainers must keep running."""
    record = _record(kube, namespace, instance)
    rollout = _rollout(record)
    previous = rollout.get("previous") or []
    if not previous:
        raise UsageError("the rollout recorded no previous objects to restore")
    documents = []
    for item in previous:
        document = copy.deepcopy(item["document"])
        document["metadata"].pop("namespace", None)
        documents.append(document)
    kube.apply(documents, namespace, server_side=server_side, force_conflicts=server_side)
    _set_phase(kube, record, "Aborting", abortedAt=inventory.now())
    return {"instance": instance, "phase": "Aborting", "restored": [object_ref(doc) for doc in documents]}


def finish(kube: Kube, namespace: str, instance: str, *, state: Path, pool_label: str) -> dict[str, Any]:
    """End the rollout once the old revision is gone and every group serves the new one."""
    record = _record(kube, namespace, instance)
    rollout = _rollout(record)
    progress = progress_of(kube, record, pool_label=pool_label)
    if progress and not _done(rollout, progress):
        raise NotReadyError(
            f"{progress.name} has not finished rolling ({', '.join(progress.revisions)})",
            hint="Follow it with `llmd rollouts status --watch`.",
        )
    Drainers(state / "rollouts" / namespace / instance).stop()
    phase = "Aborted" if rollout["phase"] == "Aborting" else "Finished"
    _set_phase(kube, record, phase, finishedAt=inventory.now())
    return {"instance": instance, "phase": phase, "revision": progress.target if progress else ""}


def watch(
    kube: Kube,
    namespace: str,
    instance: str,
    *,
    state: Path,
    pool_label: str,
    timeout: float,
    interval: float = 30,
    emit=None,
) -> dict[str, Any]:
    """Follow the roll until it is done, then finish it. A drainer that exits while old groups remain
    stops the watch: the roll would wait forever on a group nobody drains."""
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        current = status(kube, namespace, instance, state=state, pool_label=pool_label)
        line = describe_progress(current)
        if emit and line != last:
            emit(line)
            last = line
        if current["done"]:
            return finish(kube, namespace, instance, state=state, pool_label=pool_label)
        stalled = _stalled(current)
        if stalled and current["phase"] != "Paused":
            roles = ", ".join(item["role"] for item in stalled)
            logs = ", ".join(item["log"] for item in stalled)
            raise NotReadyError(
                f"the {roles} drainer exited while old {roles} groups remain; the roll waits for it",
                hint=f"Read its log ({logs}), then `llmd rollouts resume`, or `llmd rollouts abort`.",
            )
        if time.monotonic() >= deadline:
            raise NotReadyError(
                f"the roll did not finish in {timeout:.0f}s ({line})",
                hint="It keeps going with the drainers; follow it with `llmd rollouts status --watch`.",
            )
        time.sleep(interval)


def _stalled(current: dict[str, Any]) -> list[dict[str, Any]]:
    """Exited drainers whose role still has groups of a revision the roll moves away from."""
    progress = current.get("progress") or {}
    roles = progress.get("roles") or {}
    target = progress.get("target")
    return [
        drainer
        for drainer in current["drainers"]["exited"]
        if any(
            revision != target and value["groups"]
            for revision, value in (roles.get(drainer["role"]) or {}).get("byRevision", {}).items()
        )
    ]


def describe_progress(current: dict[str, Any]) -> str:
    progress = current.get("progress")
    if not progress:
        return current["phase"]
    parts = []
    for role, item in progress["roles"].items():
        new = item["byRevision"].get(progress["target"], {"groups": 0, "serving": 0})
        old = sum(
            value["groups"]
            for revision, value in item["byRevision"].items()
            if revision != progress["target"]
        )
        parts.append(f"{role} {new['serving']}/{item['replicas']} new serving, {old} old left")
    return f"{current['phase']}: " + "; ".join(parts)
