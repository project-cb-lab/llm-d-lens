"""Kubernetes Job orchestration for Model Cache downloads and deletes.

Uses one Job per "unit of storage": a single Job for ``nfs``/``dynamic-pvc``
volumes (network-shared, one copy is visible everywhere), or one Job *per
Ready node* for ``local-disk`` volumes (the hostPath directory is expected to
be replicated identically on every node — see
``docs/fern/pages/api-reference/storage.mdx`` — so each node needs its own
download/delete run, tracked and retried independently).

See ``docs/fern/pages/api-reference/model-cache.mdx`` for the full design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import os
import shlex
from pathlib import Path

from llm_d_bench.model_cache.contracts import ModelCacheEntry, ModelSourceKind, NodeDownloadStatus, TokenSourceMode
from llm_d_bench.storage.contracts import StorageVolume, StorageVolumeKind
from llm_d_bench.storage.service import discover_nodes
from llm_d_bench.utils.kubernetes import list_resources, scoped_runner

logger = logging.getLogger(__name__)

_MOUNT_PATH = "/model-cache"
# Default to a well-known, always-pullable public base image rather than a
# project-specific one (ghcr.io/llm-d-prism/model-cache-downloader) that
# nobody has actually built/published — that placeholder caused download
# Jobs to fail with ImagePullBackOff on every cluster. huggingface_hub[cli]
# is installed at container start instead (see _download_command below).
# Clusters that prebuild the image in docker/model-cache-downloader/Dockerfile
# for faster/offline starts can still override this via the env var.
_DEFAULT_IMAGE = "python:3.12-slim"
_ALL_NODES_KEY = "*"


class ModelCacheJobError(Exception):
    """Raised when a download/delete Job cannot be created."""


def _downloader_image() -> str:
    return os.environ.get("MODEL_CACHE_DOWNLOADER_IMAGE", _DEFAULT_IMAGE)


def _pvc_namespace(volume: StorageVolume) -> str:
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC and volume.dynamic_pvc:
        return volume.dynamic_pvc.namespace
    return "llm-d-bench-storage"


def _node_slug(node: str) -> str:
    return node.replace(".", "-").lower()[:20]


def _job_name(entry: ModelCacheEntry, *, node: str, action: str) -> str:
    suffix = f"-{_node_slug(node)}" if node != _ALL_NODES_KEY else ""
    return f"model-cache-{action}-{entry.id}{suffix}"[:63].rstrip("-")


def _node_from_job_name(entry: ModelCacheEntry, *, name: str, action: str) -> str | None:
    shared_name = _job_name(entry, node=_ALL_NODES_KEY, action=action)
    if name == shared_name:
        return _ALL_NODES_KEY
    prefix = f"{shared_name}-"
    if name.startswith(prefix):
        return name.removeprefix(prefix)
    return None


def _download_command(entry: ModelCacheEntry) -> list[str]:
    source = entry.source
    if source.kind != ModelSourceKind.HUGGINGFACE or not source.huggingface:
        raise ModelCacheJobError(f"unsupported model source for download: {source.kind.value}")
    download_args = [
        "hf",
        "download",
        source.huggingface.repo_id,
        "--revision",
        source.huggingface.revision,
    ]
    # Installed at container start rather than baked into the image (see
    # _DEFAULT_IMAGE above): pip no-ops quickly if a prebuilt image already
    # satisfies the requirement, so this is cheap even on a custom image.
    script = "pip install --quiet --no-cache-dir 'huggingface_hub[cli,hf_transfer]' && " + shlex.join(download_args)
    return ["sh", "-c", script]


def _delete_command(entry: ModelCacheEntry) -> list[str]:
    return ["rm", "-rf", f"{_MOUNT_PATH}/{entry.cache_path}"]


def _proxy_environment(cluster_id: str | None = None) -> list[dict]:
    """Forward proxy settings into the download/delete Job.

    Job Pods run inside the cluster and (like the model containers configured
    by deploy/runtime/composition.py's ``_model_environment``) may have no
    direct route to the public internet, so a HuggingFace/PyPI download stalls
    with "Network is unreachable" unless proxy env vars are propagated. Both
    the upper and lower-case names are checked because pip/urllib3 honor
    lower-case while many cluster tools set upper-case only.

    Prefers the owning cluster's own ``ProxyConfig`` (set via the Create
    Cluster wizard, see docs/design/cluster-creation-wizard-design.md
    section 4.2) when it is in ``mode="custom"``; otherwise falls back to the
    Prism backend process's own environment, same as before that config
    existed.
    """
    from llm_d_bench.cluster.service import resolve_proxy_env

    values = resolve_proxy_env(cluster_id)
    if not values:
        return []
    no_proxy_entries = [entry.strip() for entry in values.get("NO_PROXY", "").split(",") if entry.strip()]
    for entry in ("localhost", "127.0.0.1", ".svc", ".cluster.local"):
        if entry not in no_proxy_entries:
            no_proxy_entries.append(entry)
    values["NO_PROXY"] = ",".join(no_proxy_entries)
    return [{"name": name, "value": value} for name, value in sorted(values.items())]


def _environment(entry: ModelCacheEntry) -> list[dict]:
    environment = [
        {"name": "HF_HOME", "value": _MOUNT_PATH},
        {"name": "HF_HUB_ENABLE_HF_TRANSFER", "value": "1"},
        *_proxy_environment(entry.cluster_id),
    ]
    # Both EXISTING_SECRET and HOST modes are materialized (copied, or read
    # from the backend host's token file) into a single well-known
    # ``llm-d-hf-token`` Secret in the Job's own namespace before the Job is
    # applied -- see ``_ensure_token_secret()`` -- because a K8s
    # ``secretKeyRef`` can only ever resolve a Secret that lives in the same
    # namespace as the Pod referencing it. Referencing the *source* secret's
    # name directly here (its name/namespace as picked in the UI) would
    # silently fail to resolve whenever that source lives in a different
    # namespace than the Job (the common case, since the picker lists Secrets
    # cluster-wide) -- ``optional: True`` swallows the error and the download
    # just runs unauthenticated. This was the actual bug being fixed.
    if entry.token_source.mode in (TokenSourceMode.EXISTING_SECRET, TokenSourceMode.HOST):
        environment.append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": "llm-d-hf-token", "key": "HF_TOKEN", "optional": True}},
            }
        )
    return environment


def _job_manifest(entry: ModelCacheEntry, volume: StorageVolume, *, node: str, command: list[str], action: str) -> dict:
    namespace = _pvc_namespace(volume)
    job_name = _job_name(entry, node=node, action=action)
    pod_spec: dict = {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": action,
                "image": _downloader_image(),
                "env": _environment(entry),
                "command": command,
                "volumeMounts": [{"name": "model-cache", "mountPath": _MOUNT_PATH}],
            }
        ],
        "volumes": [{"name": "model-cache", "persistentVolumeClaim": {"claimName": volume.pvc_name}}],
    }
    if node != _ALL_NODES_KEY:
        pod_spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "prism.ai/model-cache-entry-id": entry.id,
                "prism.ai/storage-volume-id": volume.id,
                "prism.ai/model-cache-action": action,
            },
        },
        "spec": {
            "backoffLimit": 2,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": {"prism.ai/model-cache-entry-id": entry.id}}, "spec": pod_spec},
        },
    }


async def _apply_job(manifest: dict, *, cluster_id: str) -> None:
    namespace = manifest["metadata"]["namespace"]
    name = manifest["metadata"]["name"]
    runner = scoped_runner(cluster_id)
    # Jobs are immutable once created; delete any previous Job of the same name
    # first (no-op if none exists) so retries can re-apply cleanly instead of
    # failing on kubectl's "field is immutable" error. Wait for the deletion to
    # finish: Kubeflow-style ``kubectl delete --wait=false`` returns before the
    # Job (and its foreground finalizer) is gone, so an immediate ``apply`` can
    # create the Job only to have the still-in-flight delete remove it again,
    # leaving "job ... not found" on the next status/log read.
    await runner.run(
        ["kubectl", "delete", "job", name, "--namespace", namespace, "--ignore-not-found=true", "--wait=false"],
        timeout=30,
    )
    for _ in range(30):
        check = await runner.run(
            ["kubectl", "get", "job", name, "--namespace", namespace, "--output=name"],
            timeout=15,
        )
        if not check.ok:
            break
        await asyncio.sleep(1)
    result = await runner.run(
        ["kubectl", "apply", "--namespace", namespace, "--filename=-", "--output=json"],
        input=json.dumps(manifest),
        timeout=30,
    )
    if not result.ok:
        raise ModelCacheJobError((result.stderr or result.stdout or "kubectl apply failed").strip())


async def delete_jobs_for_volume(volume: StorageVolume, *, cluster_id: str) -> None:
    """Delete every download/delete Job (and its Pods) that ever mounted this volume.

    Jobs only self-clean after ``ttlSecondsAfterFinished`` (1h, see
    ``_job_manifest``), so completed/failed Jobs from earlier downloads can
    still be listed as PVC consumers (``kubectl describe pvc`` -> "Used By")
    long after they finished. Kubernetes will not let a PVC actually leave
    ``Terminating`` while any Pod still references it, so deleting a Storage
    volume that was ever used for Model Cache would otherwise hang until every
    leftover Job ages out on its own. Called from
    ``storage.service.delete_volume_objects`` before the PVC/PV are deleted.
    """
    namespace = _pvc_namespace(volume)
    runner = scoped_runner(cluster_id)
    await runner.run(
        [
            "kubectl",
            "delete",
            "job",
            "--namespace",
            namespace,
            "--ignore-not-found=true",
            "--selector",
            f"prism.ai/storage-volume-id={volume.id}",
        ],
        timeout=30,
    )


async def delete_jobs_for_entry(entry: ModelCacheEntry, *, volume: StorageVolume) -> None:
    """Best-effort delete of any download/delete Jobs for one entry.

    Unlike ``start_delete``, this never mounts the storage volume itself: it
    only removes the Job/Pod Kubernetes *objects* via the API server, which
    Kubernetes allows even while a Pod is stuck ``ContainerCreating`` on a
    volume mount that can never succeed (e.g. an NFS server rejecting the
    node). Used by ``service.request_delete`` to let an entry that never
    reached ``ready`` (so nothing was actually written to the shared cache)
    be removed immediately without waiting on a mount that may never
    complete. Errors are swallowed: this is cleanup, not correctness-critical.
    """
    namespace = _pvc_namespace(volume)
    runner = scoped_runner(entry.cluster_id)
    try:
        await runner.run(
            [
                "kubectl",
                "delete",
                "job",
                "--namespace",
                namespace,
                "--ignore-not-found=true",
                "--wait=false",
                "--selector",
                f"prism.ai/model-cache-entry-id={entry.id}",
            ],
            timeout=30,
        )
    except (TimeoutError, FileNotFoundError):
        logger.exception("event=model_cache_entry_job_cleanup_failed entry_id=%s", entry.id)


async def _ready_schedulable_node_names(cluster_id: str) -> list[str]:
    nodes = await discover_nodes(cluster_id)
    # Skip control-plane-only nodes: they're tainted NoSchedule/NoExecute and
    # these Jobs set no tolerations, so a Pod targeted at one would sit stuck
    # Pending forever (see ``StorageNodeSummary.schedulable`` and
    # ``storage/service.py::discover_nodes`` for how this is computed). A
    # single-node dev cluster whose sole node is both control-plane and
    # worker (taint removed) is still included, since it's schedulable.
    return [node.name for node in nodes if node.ready and node.schedulable]


async def _target_nodes(entry: ModelCacheEntry, volume: StorageVolume) -> list[str]:
    if volume.kind != StorageVolumeKind.LOCAL_DISK:
        return [_ALL_NODES_KEY]
    target_names = await _ready_schedulable_node_names(entry.cluster_id)
    return target_names or [_ALL_NODES_KEY]


async def _ensure_token_secret(entry: ModelCacheEntry, *, namespace: str) -> None:
    """Materialize the entry's configured HF token into the Job's own namespace.

    A Kubernetes ``secretKeyRef`` can only ever resolve a Secret living in the
    *same* namespace as the Pod referencing it, so both supported token modes
    are copied/created here into a single well-known ``llm-d-hf-token``
    Secret in the Job's namespace (``_environment()`` always references that
    fixed name) rather than referencing the user's original secret coordinates
    directly -- which would silently resolve to nothing whenever that secret
    lives in a different namespace (``optional: True`` swallows the error and
    the download just proceeds unauthenticated with no visible failure; this
    was the actual bug this function fixes).

    - ``HOST``: mirrors ``deploy/runtime/composition.py``'s
      ``create_model_secret()`` -- reads the token from a file on whichever
      host runs the Prism backend process (``~/.cache/huggingface/token``,
      written by ``huggingface-cli login``). No-op (best-effort) if that file
      doesn't exist.
    - ``EXISTING_SECRET``: mirrors ``copy_model_secret()`` -- reads the
      already-base64-encoded ``HF_TOKEN`` key from the user-selected
      ``namespace``/``name`` Secret (which may live anywhere in the cluster,
      since the picker lists Secrets cluster-wide) and re-applies it under the
      fixed name/namespace.
    - ``NONE``: no-op, download proceeds unauthenticated as configured.
    """
    token_source = entry.token_source
    runner = scoped_runner(entry.cluster_id)
    if token_source.mode == TokenSourceMode.HOST:
        token_file = Path.home() / ".cache" / "huggingface" / "token"
        if not token_file.is_file():
            return
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            return
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "llm-d-hf-token", "namespace": namespace},
            "type": "Opaque",
            "stringData": {"HF_TOKEN": token},
        }
    elif token_source.mode == TokenSourceMode.EXISTING_SECRET and token_source.namespace and token_source.name:
        read = await runner.run(
            ["kubectl", "get", "secret", token_source.name, "--namespace", token_source.namespace, "--output=json"],
            timeout=30,
        )
        if not read.ok:
            raise ModelCacheJobError(
                f"model secret {token_source.namespace}/{token_source.name} is not accessible: "
                f"{(read.stderr or read.stdout or '').strip()}"
            )
        try:
            token_b64 = json.loads(read.stdout or "{}").get("data", {}).get("HF_TOKEN")
        except json.JSONDecodeError as error:
            raise ModelCacheJobError("model secret response is invalid") from error
        if not isinstance(token_b64, str) or not token_b64:
            raise ModelCacheJobError(
                f"model secret {token_source.namespace}/{token_source.name} does not contain HF_TOKEN"
            )
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "llm-d-hf-token", "namespace": namespace},
            "type": "Opaque",
            "data": {"HF_TOKEN": token_b64},
        }
    else:
        return
    result = await runner.run(
        ["kubectl", "apply", "--namespace", namespace, "--filename=-"],
        input=json.dumps(manifest),
        timeout=30,
    )
    if not result.ok:
        raise ModelCacheJobError((result.stderr or result.stdout or "kubectl apply failed").strip())


async def launch_cache_jobs(
    entry: ModelCacheEntry, *, volume: StorageVolume, nodes: list[str],
    action: str, command_for_entry: Callable[[ModelCacheEntry], list[str]],
    append: bool = False,
) -> ModelCacheEntry:
    """Apply per-node Jobs, publishing in-progress only after all submissions succeed.

    Callers own credentials, target selection and action commands. Append mode
    preserves previously tracked nodes for incremental cache synchronization.
    """
    pending = [NodeDownloadStatus(node=node, status="pending") for node in nodes]
    entry.node_progress = list(entry.node_progress) + pending if append else pending
    for node in nodes:
        manifest = _job_manifest(entry, volume, node=node, command=command_for_entry(entry), action=action)
        await _apply_job(manifest, cluster_id=entry.cluster_id)
    for progress in entry.node_progress:
        if progress.node in nodes:
            progress.status = "downloading"  # Existing in-progress state for downloads and deletion.
    return entry


async def start_download(
    entry: ModelCacheEntry, *, volume: StorageVolume, nodes: list[str] | None = None
) -> ModelCacheEntry:
    """Create the download Job(s) for an entry and initialize its node_progress.

    ``nodes`` may be passed explicitly to re-run only a subset of nodes (used
    by retry, which re-targets just the previously-failed nodes).
    """
    await _ensure_token_secret(entry, namespace=_pvc_namespace(volume))
    nodes = nodes or await _target_nodes(entry, volume)
    return await launch_cache_jobs(
        entry, volume=volume, nodes=nodes, action="download",
        command_for_entry=_download_command,
    )


def _pending_nodes(entry: ModelCacheEntry, target_nodes: list[str]) -> list[str]:
    """Target nodes this entry has never been downloaded to."""
    known = {progress.node for progress in entry.node_progress}
    return sorted(node for node in target_nodes if node not in known and node != _ALL_NODES_KEY)


async def target_nodes(cluster_id: str, volume: StorageVolume) -> list[str]:
    """The nodes an entry's Jobs should target on ``volume``.

    Fetches the cluster's ready/schedulable nodes (one Kubernetes call); callers
    listing many entries on the same cluster should fetch this once per cluster
    and pass it to :func:`missing_nodes` instead of paying per entry.
    """
    if volume.kind != StorageVolumeKind.LOCAL_DISK:
        return [_ALL_NODES_KEY]
    return await _ready_schedulable_node_names(cluster_id)


async def missing_nodes(
    entry: ModelCacheEntry, volume: StorageVolume, *, target_nodes: list[str] | None = None
) -> list[str]:
    """Cluster nodes that can run Jobs but have never been targeted by this entry.

    Only meaningful for ``local-disk`` volumes (hostPath content is expected
    identical per-node, see ``_target_nodes``); ``nfs``/``dynamic-pvc`` share
    one copy so there is no per-node concept and this always returns ``[]``.
    A node scaled out of the cluster after being downloaded to is not
    reported here -- that's a removal, not something new to sync.

    ``target_nodes`` lets a batch caller reuse one cluster node listing across
    many entries (see ``service.compute_pending_sync``); omitting it fetches the
    listing here.
    """
    if volume.kind != StorageVolumeKind.LOCAL_DISK:
        return []
    target = target_nodes if target_nodes is not None else await _ready_schedulable_node_names(entry.cluster_id)
    return _pending_nodes(entry, target)


async def start_download_for_new_nodes(entry: ModelCacheEntry, *, volume: StorageVolume) -> ModelCacheEntry | None:
    """Extend an already-synced entry onto cluster nodes it has never targeted.

    Unlike ``start_download``, this *appends* to ``node_progress`` instead of
    replacing it, so previously-``ready`` nodes keep their recorded status
    while only the new nodes get fresh download Jobs. Returns ``None`` (no-op)
    when there is nothing new to sync.
    """
    new_nodes = await missing_nodes(entry, volume)
    if not new_nodes:
        return None
    await _ensure_token_secret(entry, namespace=_pvc_namespace(volume))
    return await launch_cache_jobs(
        entry, volume=volume, nodes=new_nodes, action="download",
        command_for_entry=_download_command, append=True,
    )


async def start_delete(
    entry: ModelCacheEntry, *, volume: StorageVolume, nodes: list[str] | None = None
) -> ModelCacheEntry:
    """Create the delete Job(s) for an entry, mirroring ``start_download``'s topology."""
    nodes = nodes or [progress.node for progress in entry.node_progress] or await _target_nodes(entry, volume)
    return await launch_cache_jobs(
        entry, volume=volume, nodes=nodes, action="delete",
        command_for_entry=_delete_command,
    )


async def ensure_delete_started(entry: ModelCacheEntry, *, volume: StorageVolume) -> ModelCacheEntry:
    """Detect and recover from a delete that flipped to ``deleting`` but never actually ran.

    ``service.request_delete()`` marks an entry ``deleting`` synchronously,
    while the actual ``start_delete()`` Job creation happens in
    ``service.provision_delete()``, fired off as a best-effort background
    ``asyncio`` task by the router. If the backend process restarts (or that
    task otherwise dies/errors) between those two steps, no delete Job is
    ever created, yet the entry is stuck at ``deleting`` forever -- the
    frontend disables its own Delete button once an entry is already
    ``deleting``, so there's no user-facing way to retry. Called lazily from
    ``service._refresh()`` on every read: if no delete Job exists for this
    entry at all, (re-)trigger ``start_delete()``, which is always safe to
    call again (``_apply_job`` deletes-then-applies by name, so it's a no-op
    for any node whose Job already exists and is still running).
    """
    namespace = _pvc_namespace(volume)
    existing = await list_resources(
        "jobs",
        namespace=namespace,
        selector=f"prism.ai/model-cache-entry-id={entry.id},prism.ai/model-cache-action=delete",
        cluster_id=entry.cluster_id,
    )
    if existing:
        return entry
    return await start_delete(entry, volume=volume)


def _job_status(job: dict) -> tuple[str, str | None]:
    status = job.get("status") or {}
    if (status.get("succeeded") or 0) >= 1:
        return "ready", None
    if (status.get("failed") or 0) >= int((job.get("spec") or {}).get("backoffLimit", 2)):
        conditions = status.get("conditions") or []
        message = next(
            (condition.get("message") for condition in conditions if condition.get("type") == "Failed"),
            None,
        )
        return "failed", (message or "job failed")[:500]
    return "downloading", None


async def poll_progress(entry: ModelCacheEntry, *, volume: StorageVolume, action: str = "download") -> ModelCacheEntry:
    """Refresh ``entry.node_progress``/``status`` from the live Job objects.

    Called both by the background task loop and lazily on read (see
    ``service.py``), so status is never staler than the last HTTP request.
    """
    namespace = _pvc_namespace(volume)
    jobs = await list_resources(
        "jobs",
        namespace=namespace,
        selector=f"prism.ai/model-cache-entry-id={entry.id},prism.ai/model-cache-action={action}",
        cluster_id=entry.cluster_id,
    )
    jobs_by_node: dict[str, dict] = {}
    for job in jobs:
        name = str((job.get("metadata") or {}).get("name") or "")
        node = next(
            (
                progress.node
                for progress in entry.node_progress
                if name == _job_name(entry, node=progress.node, action=action)
            ),
            None,
        ) or _node_from_job_name(entry, name=name, action=action)
        if node is not None:
            jobs_by_node[node] = job
    if not entry.node_progress and jobs_by_node:
        entry.node_progress = [NodeDownloadStatus(node=node, status="downloading") for node in sorted(jobs_by_node)]
    for progress in entry.node_progress:
        job = jobs_by_node.get(progress.node)
        if job is None:
            continue
        status, failure_detail = _job_status(job)
        progress.status = status
        progress.failure_detail = failure_detail
    return entry


async def tail_logs(
    entry: ModelCacheEntry, *, volume: StorageVolume, node: str, action: str = "download", tail_lines: int = 120
) -> str:
    """Return the tail of the download/delete Job's Pod logs for one node.

    Mirrors the ``kubectl logs ... --tail=120`` convention already used by
    ``deploy/providers/baseline_vllm.py``'s diagnostics helper.
    """
    namespace = _pvc_namespace(volume)
    job_name = _job_name(entry, node=node, action=action)
    runner = scoped_runner(entry.cluster_id)
    result = await runner.run(
        ["kubectl", "logs", f"job/{job_name}", "--namespace", namespace, f"--tail={tail_lines}"],
        timeout=30,
    )
    if result.ok:
        return result.stdout
    # The Job may already be gone (``ttlSecondsAfterFinished`` cleaned it up) or
    # its name may have been truncated; fall back to the Job's Pods by label so
    # recently-finished downloads can still be inspected.
    fallback = await runner.run(
        [
            "kubectl",
            "logs",
            "--namespace",
            namespace,
            "-l",
            f"prism.ai/model-cache-entry-id={entry.id},prism.ai/model-cache-action={action}",
            f"--tail={tail_lines}",
            "--all-containers=true",
            "--prefix=true",
        ],
        timeout=30,
    )
    if fallback.ok and (fallback.stdout or "").strip():
        return fallback.stdout
    detail = (fallback.stderr or result.stderr or result.stdout or "").strip()
    if "jobs.batch" in detail.lower() and "not found" in detail.lower():
        return "no logs available: the download Job has already been cleaned up"
    return detail or "no logs available"


# --- Scan for model files already present on a volume (no download needed) ---
#
# Runs a short-lived Job (one per node for ``local-disk`` volumes, one shared
# Job otherwise -- same topology as downloads/deletes, see module docstring)
# that lists the ``hub/models--*`` directories under the mount, so Storage
# can auto-register a Model Cache entry for model files that were already on
# a volume before it was registered (or added out-of-band afterwards) instead
# of requiring the user to re-request each download from scratch.

# The scan Job runs the shared downloader image, which a cold node may still
# have to pull (observed ~1 min for `python:3.12-slim`), so this must tolerate
# more than just the instant `ls`.
_SCAN_TIMEOUT_SECONDS = 300
_SCAN_POLL_SECONDS = 2


def _scan_command() -> list[str]:
    return ["sh", "-c", f"ls -1 {_MOUNT_PATH}/hub 2>/dev/null | grep '^models--' || true"]


def _scan_job_name(volume: StorageVolume, *, node: str) -> str:
    suffix = f"-{_node_slug(node)}" if node != _ALL_NODES_KEY else ""
    return f"model-cache-scan-{volume.id}{suffix}"[:63].rstrip("-")


def _scan_job_manifest(volume: StorageVolume, *, node: str) -> dict:
    namespace = _pvc_namespace(volume)
    job_name = _scan_job_name(volume, node=node)
    pod_spec: dict = {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "scan",
                "image": _downloader_image(),
                "command": _scan_command(),
                "volumeMounts": [{"name": "model-cache", "mountPath": _MOUNT_PATH}],
            }
        ],
        "volumes": [{"name": "model-cache", "persistentVolumeClaim": {"claimName": volume.pvc_name}}],
    }
    if node != _ALL_NODES_KEY:
        pod_spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "prism.ai/storage-volume-id": volume.id,
                "prism.ai/model-cache-action": "scan",
            },
        },
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": {"prism.ai/storage-volume-id": volume.id}}, "spec": pod_spec},
        },
    }


def _repo_id_from_scan_dirname(dirname: str) -> str | None:
    """Reverse ``hf_cache_path``'s ``hub/models--org--name`` layout back to ``org/name``.

    Assumes the (default, near-universal) case where neither the org nor the
    model name itself contains a literal ``--`` -- matching the same
    assumption ``hf_cache_path`` makes when writing that layout in the first
    place.
    """
    if not dirname.startswith("models--"):
        return None
    rest = dirname.removeprefix("models--")
    org, separator, name = rest.partition("--")
    if not separator or not org or not name:
        return None
    return f"{org}/{name}"


async def _await_scan_job(job_name: str, *, volume: StorageVolume, cluster_id: str) -> str:
    """Poll a scan Job until it succeeds/fails or ``_SCAN_TIMEOUT_SECONDS`` elapses."""
    namespace = _pvc_namespace(volume)
    elapsed = 0.0
    while elapsed < _SCAN_TIMEOUT_SECONDS:
        found = await list_resources(
            "jobs",
            namespace=namespace,
            selector=f"prism.ai/storage-volume-id={volume.id},prism.ai/model-cache-action=scan",
            cluster_id=cluster_id,
        )
        job = next((item for item in found if (item.get("metadata") or {}).get("name") == job_name), None)
        if job is not None:
            status, _ = _job_status(job)
            if status in ("ready", "failed"):
                return status
        await asyncio.sleep(_SCAN_POLL_SECONDS)
        elapsed += _SCAN_POLL_SECONDS
    return "timeout"


async def scan_existing_models(volume: StorageVolume, *, cluster_id: str) -> dict[str, list[str]]:
    """Discover model directories already present on a volume's storage.

    Returns ``{repo_id: [nodes that have it]}``. For ``local-disk`` volumes
    the node list reflects which cluster nodes actually have the directory
    (a partially-replicated hostPath, e.g. content only copied to some
    hosts, is reported accurately so the existing node-drift/"Sync new
    nodes" flow can complete it); for ``nfs``/``dynamic-pvc`` volumes the
    node list is always ``["*"]`` since one shared copy is visible
    everywhere. Best-effort: a node whose scan Job fails or times out is
    treated as "nothing found there" rather than failing the whole scan,
    since this is an auto-discovery convenience, not a correctness-critical
    path -- the user can always fall back to requesting the download
    manually if a model is missed.
    """
    if not volume.pvc_name:
        return {}
    nodes = (
        [_ALL_NODES_KEY]
        if volume.kind != StorageVolumeKind.LOCAL_DISK
        else (await _ready_schedulable_node_names(cluster_id) or [_ALL_NODES_KEY])
    )
    namespace = _pvc_namespace(volume)
    found: dict[str, list[str]] = {}
    for node in nodes:
        manifest = _scan_job_manifest(volume, node=node)
        job_name = manifest["metadata"]["name"]
        try:
            await _apply_job(manifest, cluster_id=cluster_id)
            status = await _await_scan_job(job_name, volume=volume, cluster_id=cluster_id)
            if status != "ready":
                continue
            runner = scoped_runner(cluster_id)
            # --tail=-1: a volume can hold many thousands of model directories;
            # one line each, so read them all rather than silently dropping the
            # ones past a fixed cap.
            result = await runner.run(
                ["kubectl", "logs", f"job/{job_name}", "--namespace", namespace, "--tail=-1"],
                timeout=30,
            )
        except (ModelCacheJobError, TimeoutError, FileNotFoundError):
            logger.exception("event=model_cache_scan_job_failed volume_id=%s node=%s", volume.id, node)
            continue
        if not result.ok:
            continue
        node_key = "*" if node == _ALL_NODES_KEY else node
        for line in result.stdout.splitlines():
            repo_id = _repo_id_from_scan_dirname(line.strip())
            if repo_id:
                found.setdefault(repo_id, []).append(node_key)
    return found
