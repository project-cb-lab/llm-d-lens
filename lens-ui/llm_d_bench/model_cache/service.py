"""Service layer wiring Model Cache requests to the store, storage volumes, and Jobs.

See ``docs/fern/pages/api-reference/model-cache.mdx`` for the full request
flow and the Storage-module delete guard this module exposes
(``list_entries_for_volume``).
"""

from __future__ import annotations

import asyncio

from llm_d_bench.model_cache import jobs
from llm_d_bench.model_cache.contracts import (
    HuggingFaceSource,
    ModelCacheCreateRequest,
    ModelCacheEntry,
    ModelCacheEntryStatus,
    ModelSource,
    ModelSourceKind,
    NodeDownloadStatus,
    TokenSource,
    TokenSourceMode,
    hf_cache_path,
)
from llm_d_bench.model_cache.jobs import ModelCacheJobError
from llm_d_bench.model_cache.store import ModelCacheStore, default_store
from llm_d_bench.storage.contracts import StorageVolume, StorageVolumeKind
from llm_d_bench.storage.service import get_ready_volume

# Bounds on how long a Storage volume delete cascade waits for each Model
# Cache entry's own delete Job to actually finish removing files before
# moving on to the next entry / the volume's own PV/PVC teardown. Best-effort
# only: ``delete_volume_objects`` force-deletes any still-running Jobs before
# it deletes the PVC regardless, so a timeout here never blocks the volume
# delete forever -- it just means that entry's files may not be fully gone
# yet when the volume disappears.
_DELETE_WAIT_TIMEOUT_SECONDS = 300
_DELETE_WAIT_POLL_SECONDS = 2

_FAILURE_STATUSES = frozenset({"failed"})
_IN_PROGRESS_STATUSES = frozenset({"pending", "downloading"})
# Entries in these lifecycle states already have a Job actively mutating
# node_progress; a node-drift check/fix would race with it, so they're
# skipped by both the read-time drift check and the sync actions below.
_BUSY_STATUSES = frozenset(
    {
        ModelCacheEntryStatus.PENDING,
        ModelCacheEntryStatus.DOWNLOADING,
        ModelCacheEntryStatus.DELETING,
    }
)


def _cluster_token_source(cluster_id: str) -> TokenSource:
    """The HF token saved with the cluster at creation time (wizard HF_TOKEN step).

    Model cache downloads follow the cluster's token so the caller never has to
    pick one; falls back to unauthenticated when the cluster has none recorded.
    """
    from llm_d_bench.cluster.registry import get_cluster  # noqa: PLC0415

    cluster = get_cluster(cluster_id)
    namespace = getattr(cluster, "hf_token_secret_namespace", None) if cluster else None
    name = getattr(cluster, "hf_token_secret_name", None) if cluster else None
    if namespace and name:
        return TokenSource(mode=TokenSourceMode.EXISTING_SECRET, namespace=namespace, name=name)
    return TokenSource(mode=TokenSourceMode.NONE)


class ModelCacheNotFoundError(Exception):
    """Raised when a model cache entry does not exist."""


class ModelCatalogNotSupportedError(Exception):
    """Raised when a request names the not-yet-implemented Model Catalog source."""


class ModelCacheInvalidStateError(Exception):
    """Raised when an operation is attempted against an entry in the wrong status."""


def _overall_status(entry: ModelCacheEntry) -> ModelCacheEntryStatus:
    statuses = {progress.status for progress in entry.node_progress}
    if not statuses:
        return entry.status
    if statuses <= {"ready"}:
        return ModelCacheEntryStatus.READY
    if statuses & _FAILURE_STATUSES and not (statuses & _IN_PROGRESS_STATUSES):
        return ModelCacheEntryStatus.FAILED
    return entry.status


async def _refresh(entry: ModelCacheEntry, *, store: ModelCacheStore) -> ModelCacheEntry | None:
    """Refresh an in-progress entry from its live Job(s); called lazily on read.

    Returns ``None`` if a delete Job finished successfully, in which case the
    entry record has already been removed from the store. Also self-heals a
    ``deleting`` entry whose delete Job was never actually created (see
    ``jobs.ensure_delete_started``), which would otherwise be stuck forever
    since the frontend disables its own Delete button once an entry is
    already ``deleting``.
    """
    if entry.status not in (ModelCacheEntryStatus.DOWNLOADING, ModelCacheEntryStatus.DELETING):
        return entry
    volume = get_or_none_volume(entry.storage_volume_id)
    if volume is None:
        return entry
    action = "delete" if entry.status == ModelCacheEntryStatus.DELETING else "download"
    if entry.status == ModelCacheEntryStatus.DELETING:
        try:
            entry = await jobs.ensure_delete_started(entry, volume=volume)
        except ModelCacheJobError as error:
            entry.status = ModelCacheEntryStatus.FAILED
            entry.failure_detail = f"delete failed: {str(error)[:480]}"
            store.save(entry)
            return entry
    entry = await jobs.poll_progress(entry, volume=volume, action=action)
    status = _overall_status(entry)
    if status == ModelCacheEntryStatus.READY and entry.status == ModelCacheEntryStatus.DELETING:
        store.delete(entry.id)
        return None
    if status != entry.status:
        entry.status = status
        if status == ModelCacheEntryStatus.FAILED:
            failed_nodes = [progress.node for progress in entry.node_progress if progress.status == "failed"]
            entry.failure_detail = f"failed on node(s): {', '.join(sorted(failed_nodes))}"
    store.save(entry)
    return entry


def get_or_none_volume(volume_id: str) -> StorageVolume | None:
    from llm_d_bench.storage.service import default_service

    return default_service().get(volume_id)


class ModelCacheService:
    """Facade combining the store, storage-volume lookups, and Job orchestration."""

    def __init__(self, store: ModelCacheStore | None = None) -> None:
        self._store = store or default_store()

    async def list(
        self, *, cluster_id: str | None = None, storage_volume_id: str | None = None
    ) -> list[ModelCacheEntry]:
        entries = []
        for entry in self._store.list():
            refreshed = await _refresh(entry, store=self._store)
            if refreshed is None:
                continue
            entries.append(refreshed)
        if cluster_id:
            entries = [entry for entry in entries if entry.cluster_id == cluster_id]
        if storage_volume_id:
            entries = [entry for entry in entries if entry.storage_volume_id == storage_volume_id]
        return entries

    async def get(self, entry_id: str) -> ModelCacheEntry:
        entry = self._store.get(entry_id)
        if entry is None:
            raise ModelCacheNotFoundError(f"model cache entry not found: {entry_id}")
        refreshed = await _refresh(entry, store=self._store)
        if refreshed is None:
            raise ModelCacheNotFoundError(f"model cache entry not found: {entry_id}")
        return refreshed

    async def request_download(self, request: ModelCacheCreateRequest) -> ModelCacheEntry:
        """Validate the request and register a pending entry (Jobs start async, see ``provision``)."""
        if request.source.kind == ModelSourceKind.MODEL_CATALOG:
            raise ModelCatalogNotSupportedError("model-catalog sources are not implemented yet")
        volume = await get_ready_volume(request.storage_volume_id, cluster_id=request.cluster_id)
        source_display = request.source.display_name()
        existing = self._store.find_by_volume_and_source(volume.id, source_display)
        if existing and existing.status in (ModelCacheEntryStatus.PENDING, ModelCacheEntryStatus.DOWNLOADING):
            return existing
        assert request.source.huggingface is not None  # enforced by ModelSourceKind check above
        # Downloads always use the cluster's saved HF token (never a user choice).
        token_source = _cluster_token_source(request.cluster_id)
        cache_path = hf_cache_path(request.source.huggingface.repo_id)
        if existing:
            existing.source = request.source
            existing.token_source = token_source
            existing.cache_path = cache_path
            existing.status = ModelCacheEntryStatus.PENDING
            existing.failure_detail = None
            existing.node_progress = []
            return self._store.save(existing)
        entry = ModelCacheEntry(
            clusterId=request.cluster_id,
            storageVolumeId=volume.id,
            source=request.source,
            tokenSource=token_source,
            cachePath=cache_path,
        )
        return self._store.create(entry)

    async def provision(self, entry_id: str) -> None:
        """Kick off the download Job(s) for a pending entry (fire-and-forget background task)."""
        entry = self._store.get(entry_id)
        if entry is None or entry.status != ModelCacheEntryStatus.PENDING:
            return
        volume = get_or_none_volume(entry.storage_volume_id)
        if volume is None:
            entry.status = ModelCacheEntryStatus.FAILED
            entry.failure_detail = "storage volume no longer exists"
            self._store.save(entry)
            return
        entry.status = ModelCacheEntryStatus.DOWNLOADING
        self._store.save(entry)
        # ``retry()`` trims node_progress down to only the previously-failed
        # nodes before flipping the entry back to PENDING; forward that
        # restriction here so a retry only re-downloads on those nodes
        # instead of re-targeting every ready node from scratch.
        retry_nodes = [progress.node for progress in entry.node_progress] or None
        try:
            entry = await jobs.start_download(entry, volume=volume, nodes=retry_nodes)
        except ModelCacheJobError as error:
            entry.status = ModelCacheEntryStatus.FAILED
            entry.failure_detail = str(error)[:500]
        self._store.save(entry)

    def retry(self, entry_id: str) -> ModelCacheEntry:
        entry = self._store.get(entry_id)
        if entry is None:
            raise ModelCacheNotFoundError(f"model cache entry not found: {entry_id}")
        if entry.status != ModelCacheEntryStatus.FAILED:
            raise ModelCacheInvalidStateError("only failed entries can be retried")
        failed_nodes = [progress.node for progress in entry.node_progress if progress.status == "failed"]
        entry.node_progress = [progress for progress in entry.node_progress if progress.node in failed_nodes]
        entry.status = ModelCacheEntryStatus.PENDING
        entry.failure_detail = None
        return self._store.save(entry)

    async def request_delete(self, entry_id: str, *, keep_files: bool = False) -> ModelCacheEntry | None:
        """Delete an entry, skipping the mount-dependent cleanup Job when safe.

        An entry that never reached ``ready`` on any node has no confirmed
        data written to the shared cache path (it's still ``pending``,
        mid-``downloading``, or ``failed`` before ever completing), so there
        is nothing on the volume that requires mounting it to clean up.
        Removing it immediately (after best-effort deleting any of its
        download Jobs, which is a plain Kubernetes-API object delete and
        never itself requires the volume to mount) avoids forcing the user
        to wait on a delete Job that would mount the same volume a failed
        download Job already couldn't -- e.g. an NFS server rejecting the
        node. Only an entry that has actually written data (``ready`` on at
        least one node) goes through the ``deleting`` + ``provision_delete``
        Job flow, since that's the only case where real cleanup is needed.

        ``keep_files=True`` (the Storage volume delete flow's "keep model
        files" choice) takes the same immediate-removal path regardless of
        ``ever_ready``: the Model Cache *record* is dropped, but no cleanup
        Job that would ``rm -rf`` the model's files on the shared
        NFS/hostPath storage is ever created.

        Returns ``None`` when the entry was removed synchronously here.
        """
        entry = self._store.get(entry_id)
        if entry is None:
            raise ModelCacheNotFoundError(f"model cache entry not found: {entry_id}")
        ever_ready = entry.status == ModelCacheEntryStatus.READY or any(
            progress.status == "ready" for progress in entry.node_progress
        )
        if keep_files or not ever_ready:
            # Applies even if a previous request already flipped this entry to
            # ``deleting`` (e.g. before this self-heal existed, or a prior
            # attempt got stuck the same way) -- there's still nothing ``ready``
            # recorded, so it's still safe to remove immediately.
            volume = get_or_none_volume(entry.storage_volume_id)
            if volume is not None:
                await jobs.delete_jobs_for_entry(entry, volume=volume)
            self._store.delete(entry.id)
            return None
        if entry.status == ModelCacheEntryStatus.DELETING:
            return entry
        entry.status = ModelCacheEntryStatus.DELETING
        entry.failure_detail = None
        return self._store.save(entry)

    async def provision_delete(self, entry_id: str) -> None:
        entry = self._store.get(entry_id)
        if entry is None or entry.status != ModelCacheEntryStatus.DELETING:
            return
        volume = get_or_none_volume(entry.storage_volume_id)
        if volume is None:
            # Storage volume is already gone; nothing left to clean up on disk.
            self._store.delete(entry.id)
            return
        try:
            entry = await jobs.start_delete(entry, volume=volume)
            self._store.save(entry)
        except ModelCacheJobError as error:
            entry.status = ModelCacheEntryStatus.FAILED
            entry.failure_detail = f"delete failed: {str(error)[:480]}"
            self._store.save(entry)

    async def compute_pending_sync(self, entries: list[ModelCacheEntry]) -> dict[str, list[str]]:
        """Return, per entry, cluster nodes never targeted by its downloads.

        Skips entries that already have a Job in flight (``_BUSY_STATUSES``)
        and volumes that are not ``local-disk`` (no per-node concept there).
        Node listings are cached per cluster id within this call so a batch
        of entries on the same cluster only pays for one ``kubectl get
        nodes`` regardless of how many entries/volumes it covers.
        """
        pending: dict[str, list[str]] = {}
        volume_cache: dict[str, StorageVolume | None] = {}
        node_cache: dict[str, list[str]] = {}
        for entry in entries:
            if entry.status in _BUSY_STATUSES:
                continue
            if entry.storage_volume_id not in volume_cache:
                volume_cache[entry.storage_volume_id] = get_or_none_volume(entry.storage_volume_id)
            volume = volume_cache[entry.storage_volume_id]
            if volume is None or volume.kind != StorageVolumeKind.LOCAL_DISK:
                continue
            if entry.cluster_id not in node_cache:
                node_cache[entry.cluster_id] = await jobs.target_nodes(entry.cluster_id, volume)
            missing = await jobs.missing_nodes(entry, volume, target_nodes=node_cache[entry.cluster_id])
            if missing:
                pending[entry.id] = missing
        return pending

    async def sync_nodes(self, entry_id: str) -> ModelCacheEntry:
        """Flip an entry back to DOWNLOADING if it has unsynced cluster nodes.

        The actual Job creation happens in ``provision_new_nodes`` (fired as
        a background task by the router, mirroring ``request_download``),
        so this only needs to do the synchronous state transition.
        """
        entry = self._store.get(entry_id)
        if entry is None:
            raise ModelCacheNotFoundError(f"model cache entry not found: {entry_id}")
        if entry.status in _BUSY_STATUSES:
            return entry
        volume = get_or_none_volume(entry.storage_volume_id)
        if volume is None or volume.kind != StorageVolumeKind.LOCAL_DISK:
            return entry
        missing = await jobs.missing_nodes(entry, volume)
        if not missing:
            return entry
        entry.status = ModelCacheEntryStatus.DOWNLOADING
        return self._store.save(entry)

    async def provision_new_nodes(self, entry_id: str) -> None:
        entry = self._store.get(entry_id)
        if entry is None or entry.status != ModelCacheEntryStatus.DOWNLOADING:
            return
        volume = get_or_none_volume(entry.storage_volume_id)
        if volume is None:
            entry.status = ModelCacheEntryStatus.FAILED
            entry.failure_detail = "storage volume no longer exists"
            self._store.save(entry)
            return
        try:
            updated = await jobs.start_download_for_new_nodes(entry, volume=volume)
        except ModelCacheJobError as error:
            entry.status = ModelCacheEntryStatus.FAILED
            entry.failure_detail = str(error)[:500]
            self._store.save(entry)
            return
        # ``updated`` is None if another reader already synced this entry's
        # nodes between ``sync_nodes`` and here (or it had nothing to sync
        # after all); restore it to whatever _refresh() would compute next
        # read rather than leaving it stuck at DOWNLOADING with no Jobs.
        if updated is None:
            entry.status = _overall_status(entry)
            self._store.save(entry)
            return
        self._store.save(updated)

    async def sync_all_nodes(self, *, cluster_id: str | None = None) -> list[ModelCacheEntry]:
        """One-click fix for every entry whose volume gained cluster nodes it
        was never synced to. Flips each affected entry to DOWNLOADING
        synchronously; callers are expected to fire ``provision_new_nodes``
        for each returned entry as a background task, exactly like a single
        ``sync_nodes`` call.
        """
        changed: list[ModelCacheEntry] = []
        for entry in self._store.list():
            if cluster_id and entry.cluster_id != cluster_id:
                continue
            synced = await self.sync_nodes(entry.id)
            if synced.status == ModelCacheEntryStatus.DOWNLOADING:
                changed.append(synced)
        return changed

    def list_entries_for_volume(self, storage_volume_id: str) -> list[ModelCacheEntry]:
        """Used by ``storage.service.delete`` to reject deleting a volume still in use."""
        return self._store.list_for_volume(storage_volume_id)

    async def _wait_until_removed(self, entry_id: str) -> None:
        """Best-effort wait for a ``deleting`` entry's cleanup Job to finish removing it.

        Polls ``get()`` (which itself polls the live delete Job via
        ``_refresh``/``jobs.poll_progress`` and removes the store record once
        the Job succeeds) until the entry disappears or ``_DELETE_WAIT_TIMEOUT_SECONDS``
        elapses. Used by ``delete_entries_for_volume`` so a Storage volume
        delete gives each Model Cache entry's real file cleanup a fair chance
        to finish before the volume's own PV/PVC are torn down -- but never
        blocks the volume delete forever, since ``delete_volume_objects``
        force-deletes any still-running Jobs before it deletes the PVC anyway.
        """
        elapsed = 0.0
        while elapsed < _DELETE_WAIT_TIMEOUT_SECONDS:
            try:
                await self.get(entry_id)
            except ModelCacheNotFoundError:
                return
            await asyncio.sleep(_DELETE_WAIT_POLL_SECONDS)
            elapsed += _DELETE_WAIT_POLL_SECONDS

    async def delete_entries_for_volume(self, storage_volume_id: str, *, keep_files: bool) -> None:
        """Cascade-delete every Model Cache entry on a volume being deleted.

        ``keep_files=True`` removes each entry's record without touching the
        model files on the underlying NFS/hostPath storage (see
        ``request_delete``). ``keep_files=False`` actually deletes the model
        files first (one entry at a time, waiting for each to finish) before
        the caller (``storage.service.delete``) tears down the volume itself.
        """
        for entry in self.list_entries_for_volume(storage_volume_id):
            remaining = await self.request_delete(entry.id, keep_files=keep_files)
            if remaining is not None:
                await self.provision_delete(remaining.id)
                await self._wait_until_removed(remaining.id)

    async def import_existing_models(self, volume: StorageVolume) -> list[ModelCacheEntry]:
        """Auto-register Model Cache entries for model files already present on a volume.

        Called once a Storage volume becomes ``ready`` (and can be re-run any
        time, e.g. after files were added to an NFS export out-of-band): scans
        the volume for ``hub/models--org--name`` directories (the same layout
        ``request_download`` writes into, see ``hf_cache_path``) and creates a
        ``ready`` entry for each repo id found that isn't already tracked.

        For a ``local-disk`` volume where only some nodes have the files
        (e.g. content was only ever copied onto a subset of hosts), the new
        entry's ``node_progress`` only lists the nodes that actually have it;
        the existing node-drift/"Sync new nodes" machinery
        (``compute_pending_sync``/``sync_nodes``) then detects the other
        cluster nodes are missing it and lets the user backfill them with the
        same one-click action used for regular downloads. The model's exact
        revision cannot be recovered from the directory layout alone, so
        imported entries default to ``revision="main"``.
        """
        found = await jobs.scan_existing_models(volume, cluster_id=volume.cluster_id)
        created: list[ModelCacheEntry] = []
        for repo_id, nodes_with_it in found.items():
            source = ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId=repo_id))
            source_display = source.display_name()
            if self._store.find_by_volume_and_source(volume.id, source_display) is not None:
                continue
            node_progress = (
                [NodeDownloadStatus(node=node, status="ready") for node in sorted(nodes_with_it)]
                if volume.kind == StorageVolumeKind.LOCAL_DISK
                else []
            )
            entry = ModelCacheEntry(
                clusterId=volume.cluster_id,
                storageVolumeId=volume.id,
                source=source,
                cachePath=hf_cache_path(repo_id),
                status=ModelCacheEntryStatus.READY,
                nodeProgress=node_progress,
            )
            created.append(self._store.create(entry))
        return created

    async def get_logs(self, entry_id: str, *, node: str | None = None) -> str:
        """Return the tail of the most relevant Job's logs for an entry.

        Defaults to the first node's Job (or the shared ``*`` Job for
        nfs/dynamic-pvc volumes) when ``node`` is not given.
        """
        entry = self._store.get(entry_id)
        if entry is None:
            raise ModelCacheNotFoundError(f"model cache entry not found: {entry_id}")
        volume = get_or_none_volume(entry.storage_volume_id)
        if volume is None:
            raise ModelCacheNotFoundError(f"storage volume no longer exists: {entry.storage_volume_id}")
        target_node = node or (entry.node_progress[0].node if entry.node_progress else "*")
        action = "delete" if entry.status == ModelCacheEntryStatus.DELETING else "download"
        return await jobs.tail_logs(entry, volume=volume, node=target_node, action=action)


def default_service() -> ModelCacheService:
    return ModelCacheService(default_store())


def list_entries_for_volume(storage_volume_id: str) -> list[ModelCacheEntry]:
    """Module-level convenience wrapper so ``storage/service.py`` need not build a facade."""
    return default_service().list_entries_for_volume(storage_volume_id)


async def delete_entries_for_volume(storage_volume_id: str, *, keep_files: bool) -> None:
    """Module-level convenience wrapper used by ``storage.service.finish_delete``."""
    await default_service().delete_entries_for_volume(storage_volume_id, keep_files=keep_files)


async def import_existing_models(volume: StorageVolume) -> list[ModelCacheEntry]:
    """Module-level convenience wrapper used by ``storage.router`` after a volume becomes ready."""
    return await default_service().import_existing_models(volume)
