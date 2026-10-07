"""Tests for the Model Cache service layer."""

from types import SimpleNamespace

import pytest

from llm_d_bench.model_cache import jobs as jobs_module
from llm_d_bench.model_cache import service as service_module
from llm_d_bench.model_cache.contracts import (
    HuggingFaceSource,
    ModelCacheCreateRequest,
    ModelCacheEntryStatus,
    ModelSource,
    ModelSourceKind,
    NodeDownloadStatus,
    TokenSourceMode,
)
from llm_d_bench.model_cache.service import (
    ModelCacheInvalidStateError,
    ModelCacheNotFoundError,
    ModelCacheService,
    ModelCatalogNotSupportedError,
)
from llm_d_bench.model_cache.store import ModelCacheStore
from llm_d_bench.storage.contracts import LocalDiskSpec, StorageVolume, StorageVolumeKind, StorageVolumeStatus


def _volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "status": StorageVolumeStatus.READY,
        "capacity": "100Gi",
        "readOnly": False,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
        "pvcName": "prism-storage-abc",
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def _request(**overrides) -> ModelCacheCreateRequest:
    defaults = {
        "clusterId": "cluster-1",
        "storageVolumeId": "vol-1",
        "source": ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
    }
    defaults.update(overrides)
    return ModelCacheCreateRequest(**defaults)


@pytest.fixture()
def service(tmp_path) -> ModelCacheService:
    return ModelCacheService(ModelCacheStore(tmp_path))


@pytest.mark.asyncio
async def test_request_download_rejects_model_catalog(service):
    request = _request(source=ModelSource(kind=ModelSourceKind.MODEL_CATALOG, modelCatalog={"catalogEntryId": "abc"}))
    with pytest.raises(ModelCatalogNotSupportedError):
        await service.request_download(request)


@pytest.mark.asyncio
async def test_request_download_validates_volume_readiness(monkeypatch, service):
    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        raise ValueError(f"storage volume {volume_id} is not ready (status=pending)")

    monkeypatch.setattr(service_module, "get_ready_volume", fake_get_ready_volume)
    with pytest.raises(ValueError, match="not ready"):
        await service.request_download(_request())


@pytest.mark.asyncio
async def test_request_download_creates_pending_entry(monkeypatch, service):
    volume = _volume()

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    monkeypatch.setattr(service_module, "get_ready_volume", fake_get_ready_volume)
    entry = await service.request_download(_request(storageVolumeId=volume.id))
    assert entry.status == ModelCacheEntryStatus.PENDING
    assert entry.cache_path == "hub/models--org--model"
    assert entry.storage_volume_id == volume.id


@pytest.mark.asyncio
async def test_request_download_is_idempotent_for_in_progress_entries(monkeypatch, service):
    volume = _volume()

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    monkeypatch.setattr(service_module, "get_ready_volume", fake_get_ready_volume)
    first = await service.request_download(_request(storageVolumeId=volume.id))
    second = await service.request_download(_request(storageVolumeId=volume.id))
    assert first.id == second.id
    assert len(service._store.list()) == 1


@pytest.mark.asyncio
async def test_provision_starts_download_job_and_marks_downloading(monkeypatch, service):
    volume = _volume()
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_start_download(entry, *, volume, nodes=None):
        entry.node_progress = [NodeDownloadStatus(node="*", status="downloading")]
        return entry

    monkeypatch.setattr(jobs_module, "start_download", fake_start_download)

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    monkeypatch.setattr(service_module, "get_ready_volume", fake_get_ready_volume)
    entry = await service.request_download(_request(storageVolumeId=volume.id))
    await service.provision(entry.id)
    saved = service._store.get(entry.id)
    assert saved.status == ModelCacheEntryStatus.DOWNLOADING
    assert saved.node_progress[0].node == "*"


@pytest.mark.asyncio
async def test_provision_marks_failed_on_job_error(monkeypatch, service):
    volume = _volume()
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_start_download(entry, *, volume, nodes=None):
        raise jobs_module.ModelCacheJobError("boom")

    monkeypatch.setattr(jobs_module, "start_download", fake_start_download)

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    monkeypatch.setattr(service_module, "get_ready_volume", fake_get_ready_volume)
    entry = await service.request_download(_request(storageVolumeId=volume.id))
    await service.provision(entry.id)
    saved = service._store.get(entry.id)
    assert saved.status == ModelCacheEntryStatus.FAILED
    assert "boom" in saved.failure_detail


@pytest.mark.asyncio
async def test_get_refreshes_downloading_entry_to_ready(monkeypatch, service):
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DOWNLOADING,
        nodeProgress=[NodeDownloadStatus(node="*", status="downloading")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_poll_progress(entry, *, volume, action):
        entry.node_progress = [NodeDownloadStatus(node="*", status="ready")]
        return entry

    monkeypatch.setattr(jobs_module, "poll_progress", fake_poll_progress)
    refreshed = await service.get(entry.id)
    assert refreshed.status == ModelCacheEntryStatus.READY


@pytest.mark.asyncio
async def test_get_raises_not_found(service):
    with pytest.raises(ModelCacheNotFoundError):
        await service.get("does-not-exist")


@pytest.mark.asyncio
async def test_request_delete_then_provision_delete_removes_entry(monkeypatch, service):
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="*", status="ready")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_start_delete(entry, *, volume):
        entry.node_progress = [NodeDownloadStatus(node="*", status="downloading")]
        return entry

    monkeypatch.setattr(jobs_module, "start_delete", fake_start_delete)

    await service.request_delete(entry.id)
    assert service._store.get(entry.id).status == ModelCacheEntryStatus.DELETING
    await service.provision_delete(entry.id)
    assert service._store.get(entry.id).node_progress[0].status == "downloading"


@pytest.mark.asyncio
async def test_request_delete_removes_pending_entry_immediately_without_a_job(monkeypatch, service):
    """An entry that never reached ``ready`` on any node has nothing written
    to the shared volume yet, so deleting it must not depend on mounting
    that volume (e.g. via a delete Job) -- it should be removed synchronously.
    Reproduces a real report: a brand-new entry whose download Job's Pod was
    stuck ``ContainerCreating`` (NFS server rejecting the mount) could not be
    deleted because the old code always started a delete Job that needed the
    exact same broken mount.
    """
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DOWNLOADING,
        nodeProgress=[NodeDownloadStatus(node="*", status="downloading")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    delete_jobs_calls = []

    async def fake_delete_jobs_for_entry(entry, *, volume):
        delete_jobs_calls.append(entry.id)

    monkeypatch.setattr(jobs_module, "delete_jobs_for_entry", fake_delete_jobs_for_entry)

    result = await service.request_delete(entry.id)

    assert result is None
    assert service._store.get(entry.id) is None
    assert delete_jobs_calls == [entry.id]


@pytest.mark.asyncio
async def test_request_delete_with_keep_files_removes_ready_entry_without_a_delete_job(monkeypatch, service):
    """``keep_files=True`` (the Storage volume "keep model files" choice) must
    drop the record without ever creating a delete Job -- even for an entry
    that reached ``ready`` and would normally go through the mount-dependent
    ``deleting`` + ``provision_delete`` flow.
    """
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="*", status="ready")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    start_delete_calls = []

    async def fake_start_delete(entry, *, volume):
        start_delete_calls.append(entry.id)
        return entry

    monkeypatch.setattr(jobs_module, "start_delete", fake_start_delete)

    async def fake_delete_jobs_for_entry(entry, *, volume):
        return None

    monkeypatch.setattr(jobs_module, "delete_jobs_for_entry", fake_delete_jobs_for_entry)

    result = await service.request_delete(entry.id, keep_files=True)

    assert result is None
    assert service._store.get(entry.id) is None
    assert start_delete_calls == []  # no cleanup Job that would rm -rf the files


@pytest.mark.asyncio
async def test_delete_entries_for_volume_keeps_files_for_every_entry(monkeypatch, service):
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    ready_entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/ready-model")),
        cachePath="models--org--ready-model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="*", status="ready")],
    )
    pending_entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/pending-model")),
        cachePath="models--org--pending-model",
        status=ModelCacheEntryStatus.PENDING,
    )
    service._store.create(ready_entry)
    service._store.create(pending_entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)
    monkeypatch.setattr(jobs_module, "delete_jobs_for_entry", lambda entry, *, volume: _noop())

    await service.delete_entries_for_volume(volume.id, keep_files=True)

    assert service._store.get(ready_entry.id) is None
    assert service._store.get(pending_entry.id) is None


async def _noop():
    return None


@pytest.mark.asyncio
async def test_delete_entries_for_volume_deletes_files_and_waits_for_completion(monkeypatch, service):
    """``keep_files=False`` must actually run the delete Job flow and wait
    for it to finish removing the entry before returning."""
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="*", status="ready")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)
    monkeypatch.setattr(service_module, "_DELETE_WAIT_POLL_SECONDS", 0)

    async def fake_start_delete(entry, *, volume):
        entry.node_progress = [NodeDownloadStatus(node="*", status="downloading")]
        return entry

    monkeypatch.setattr(jobs_module, "start_delete", fake_start_delete)

    async def fake_ensure_delete_started(entry, *, volume):
        # Simulate the delete Job having already been created by provision_delete.
        return entry

    monkeypatch.setattr(jobs_module, "ensure_delete_started", fake_ensure_delete_started)

    poll_calls = {"count": 0}

    async def fake_poll_progress(entry, *, volume, action="download"):
        poll_calls["count"] += 1
        # Finish the delete Job on the second poll so the wait loop actually exercises >1 iteration.
        if poll_calls["count"] >= 2:
            entry.node_progress = [NodeDownloadStatus(node="*", status="ready")]
        return entry

    monkeypatch.setattr(jobs_module, "poll_progress", fake_poll_progress)

    await service.delete_entries_for_volume(volume.id, keep_files=False)

    assert service._store.get(entry.id) is None
    assert poll_calls["count"] >= 2


@pytest.mark.asyncio
async def test_import_existing_models_creates_ready_entries_for_discovered_repos(monkeypatch, service):
    volume = _volume(kind=StorageVolumeKind.NFS, localDisk=None)

    async def fake_scan(volume, *, cluster_id):
        return {"org/model-a": ["*"], "org/model-b": ["*"]}

    monkeypatch.setattr(jobs_module, "scan_existing_models", fake_scan)

    created = await service.import_existing_models(volume)

    assert {entry.source.huggingface.repo_id for entry in created} == {"org/model-a", "org/model-b"}
    for entry in created:
        assert entry.status == ModelCacheEntryStatus.READY
        assert entry.storage_volume_id == volume.id


@pytest.mark.asyncio
async def test_import_existing_models_reports_partial_local_disk_coverage(monkeypatch, service):
    """A local-disk volume with the model only on some nodes must record only
    those nodes as ``ready`` so the existing node-drift/"Sync new nodes" flow
    detects the other cluster nodes are still missing it."""
    volume = _volume()  # local-disk, per _volume()'s defaults

    async def fake_scan(volume, *, cluster_id):
        return {"org/model": ["node-a"]}

    monkeypatch.setattr(jobs_module, "scan_existing_models", fake_scan)

    created = await service.import_existing_models(volume)

    assert len(created) == 1
    entry = created[0]
    assert [progress.node for progress in entry.node_progress] == ["node-a"]
    assert entry.status == ModelCacheEntryStatus.READY


@pytest.mark.asyncio
async def test_import_existing_models_skips_already_tracked_repos(monkeypatch, service):
    volume = _volume(kind=StorageVolumeKind.NFS, localDisk=None)
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    existing = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
    )
    service._store.create(existing)

    async def fake_scan(volume, *, cluster_id):
        return {"org/model": ["*"]}

    monkeypatch.setattr(jobs_module, "scan_existing_models", fake_scan)

    created = await service.import_existing_models(volume)

    assert created == []
    assert len(service._store.list_for_volume(volume.id)) == 1


@pytest.mark.asyncio
async def test_get_after_delete_job_succeeds_raises_not_found(monkeypatch, service):
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DELETING,
        nodeProgress=[NodeDownloadStatus(node="*", status="downloading")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_ensure_delete_started(entry, *, volume):
        return entry

    monkeypatch.setattr(jobs_module, "ensure_delete_started", fake_ensure_delete_started)

    async def fake_poll_progress(entry, *, volume, action):
        entry.node_progress = [NodeDownloadStatus(node="*", status="ready")]
        return entry

    monkeypatch.setattr(jobs_module, "poll_progress", fake_poll_progress)
    with pytest.raises(ModelCacheNotFoundError):
        await service.get(entry.id)
    assert service._store.get(entry.id) is None


@pytest.mark.asyncio
async def test_get_self_heals_delete_that_never_started(monkeypatch, service):
    """Reproduces a real stuck-forever bug: request_delete() flips status to
    DELETING synchronously, but if the fire-and-forget provision_delete()
    background task never runs (e.g. backend process restart), no delete Job
    is ever created and node_progress is left with stale pre-delete values.
    The frontend disables its Delete button once status is already
    "deleting", so the only way to recover is for a GET/list to detect the
    missing Job and re-trigger start_delete() automatically.
    """
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DELETING,
        # Stale statuses left over from before the delete was requested --
        # the delete Job was never actually created for these nodes.
        nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_ensure_delete_started(entry, *, volume):
        # Simulates jobs.ensure_delete_started() detecting no delete Job
        # exists yet and (re-)creating it via start_delete().
        entry.node_progress = [NodeDownloadStatus(node="node-1", status="downloading")]
        return entry

    monkeypatch.setattr(jobs_module, "ensure_delete_started", fake_ensure_delete_started)

    async def fake_poll_progress(entry, *, volume, action):
        return entry

    monkeypatch.setattr(jobs_module, "poll_progress", fake_poll_progress)

    refreshed = await service.get(entry.id)
    assert refreshed.status == ModelCacheEntryStatus.DELETING
    assert refreshed.node_progress[0].status == "downloading"


@pytest.mark.asyncio
async def test_get_marks_failed_when_delete_self_heal_raises(monkeypatch, service):
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DELETING,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_ensure_delete_started(entry, *, volume):
        raise jobs_module.ModelCacheJobError("kubectl apply failed")

    monkeypatch.setattr(jobs_module, "ensure_delete_started", fake_ensure_delete_started)

    refreshed = await service.get(entry.id)
    assert refreshed.status == ModelCacheEntryStatus.FAILED
    assert "delete failed" in refreshed.failure_detail


def test_retry_rejects_non_failed_entries(service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
    )
    service._store.create(entry)
    with pytest.raises(ModelCacheInvalidStateError):
        service.retry(entry.id)


def test_retry_resets_only_failed_nodes(service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.FAILED,
        nodeProgress=[
            NodeDownloadStatus(node="node-1", status="ready"),
            NodeDownloadStatus(node="node-2", status="failed"),
        ],
    )
    service._store.create(entry)
    retried = service.retry(entry.id)
    assert retried.status == ModelCacheEntryStatus.PENDING
    assert [progress.node for progress in retried.node_progress] == ["node-2"]


@pytest.mark.asyncio
async def test_provision_after_retry_only_targets_previously_failed_nodes(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.FAILED,
        nodeProgress=[
            NodeDownloadStatus(node="node-1", status="ready"),
            NodeDownloadStatus(node="node-2", status="failed"),
        ],
    )
    service._store.create(entry)
    service.retry(entry.id)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    seen_nodes = {}

    async def fake_start_download(entry, *, volume, nodes=None):
        seen_nodes["nodes"] = nodes
        entry.node_progress = [NodeDownloadStatus(node=node, status="downloading") for node in nodes]
        return entry

    monkeypatch.setattr(jobs_module, "start_download", fake_start_download)

    await service.provision(entry.id)

    assert seen_nodes["nodes"] == ["node-2"]
    saved = service._store.get(entry.id)
    assert [progress.node for progress in saved.node_progress] == ["node-2"]


def test_list_entries_for_volume(service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry1 = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
    )
    entry2 = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId="vol-2",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model2")),
        cachePath="models--org--model2",
    )
    service._store.create(entry1)
    service._store.create(entry2)
    assert [entry.id for entry in service.list_entries_for_volume("vol-1")] == [entry1.id]


@pytest.mark.asyncio
async def test_get_logs_returns_tail_from_jobs(monkeypatch, service):
    volume = _volume()
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DOWNLOADING,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="downloading")],
    )
    service._store.create(entry)

    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_tail_logs(entry, *, volume, node, action, tail_lines=120):
        assert node == "node-1"
        assert action == "download"
        return "log output"

    monkeypatch.setattr(jobs_module, "tail_logs", fake_tail_logs)
    logs = await service.get_logs(entry.id)
    assert logs == "log output"


@pytest.mark.asyncio
async def test_get_logs_raises_not_found(service):
    with pytest.raises(ModelCacheNotFoundError):
        await service.get_logs("does-not-exist")


@pytest.mark.asyncio
async def test_compute_pending_sync_reports_missing_nodes(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
    )
    service._store.create(entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_target_nodes(cluster_id, volume):
        return ["node-2"]

    async def fake_missing_nodes(entry, volume, *, target_nodes=None):
        return ["node-2"]

    monkeypatch.setattr(jobs_module, "target_nodes", fake_target_nodes)
    monkeypatch.setattr(jobs_module, "missing_nodes", fake_missing_nodes)
    pending = await service.compute_pending_sync([entry])
    assert pending == {entry.id: ["node-2"]}


@pytest.mark.asyncio
async def test_compute_pending_sync_lists_cluster_nodes_once(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entries = []
    for index in range(5):
        entry = ModelCacheEntry(
            clusterId="cluster-1",
            storageVolumeId=volume.id,
            source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId=f"org/model-{index}")),
            cachePath=f"models--org--model-{index}",
            status=ModelCacheEntryStatus.READY,
            nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
        )
        service._store.create(entry)
        entries.append(entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    calls: list[str] = []

    async def fake_target_nodes(cluster_id, volume):
        calls.append(cluster_id)
        return ["node-1", "node-2"]

    async def fake_missing_nodes(entry, volume, *, target_nodes=None):
        known = {progress.node for progress in entry.node_progress}
        return [node for node in (target_nodes or []) if node not in known]

    monkeypatch.setattr(jobs_module, "target_nodes", fake_target_nodes)
    monkeypatch.setattr(jobs_module, "missing_nodes", fake_missing_nodes)
    pending = await service.compute_pending_sync(entries)
    assert calls == ["cluster-1"]
    assert all(pending[entry.id] == ["node-2"] for entry in entries)


@pytest.mark.asyncio
async def test_compute_pending_sync_skips_busy_entries(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DOWNLOADING,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="downloading")],
    )
    service._store.create(entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fail_missing_nodes(entry, volume, *, target_nodes=None):
        raise AssertionError("missing_nodes should not be called for busy entries")

    monkeypatch.setattr(jobs_module, "missing_nodes", fail_missing_nodes)
    pending = await service.compute_pending_sync([entry])
    assert pending == {}


@pytest.mark.asyncio
async def test_sync_nodes_flips_to_downloading_when_nodes_missing(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
    )
    service._store.create(entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)
    monkeypatch.setattr(jobs_module, "missing_nodes", lambda entry, volume: _async_result(["node-2"]))

    updated = await service.sync_nodes(entry.id)
    assert updated.status == ModelCacheEntryStatus.DOWNLOADING
    assert service._store.get(entry.id).status == ModelCacheEntryStatus.DOWNLOADING


@pytest.mark.asyncio
async def test_sync_nodes_is_noop_when_nothing_missing(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
    )
    service._store.create(entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)
    monkeypatch.setattr(jobs_module, "missing_nodes", lambda entry, volume: _async_result([]))

    updated = await service.sync_nodes(entry.id)
    assert updated.status == ModelCacheEntryStatus.READY


@pytest.mark.asyncio
async def test_sync_nodes_raises_not_found(service):
    with pytest.raises(ModelCacheNotFoundError):
        await service.sync_nodes("does-not-exist")


@pytest.mark.asyncio
async def test_provision_new_nodes_creates_jobs_for_missing_nodes_only(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        cachePath="models--org--model",
        status=ModelCacheEntryStatus.DOWNLOADING,
        nodeProgress=[
            NodeDownloadStatus(node="node-1", status="ready"),
            NodeDownloadStatus(node="node-2", status="pending"),
        ],
    )
    service._store.create(entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_start_download_for_new_nodes(entry, *, volume):
        entry.node_progress = [
            NodeDownloadStatus(node="node-1", status="ready"),
            NodeDownloadStatus(node="node-2", status="downloading"),
        ]
        return entry

    monkeypatch.setattr(jobs_module, "start_download_for_new_nodes", fake_start_download_for_new_nodes)
    await service.provision_new_nodes(entry.id)
    saved = service._store.get(entry.id)
    assert {progress.node: progress.status for progress in saved.node_progress} == {
        "node-1": "ready",
        "node-2": "downloading",
    }


@pytest.mark.asyncio
async def test_sync_all_nodes_only_returns_changed_entries(monkeypatch, service):
    from llm_d_bench.model_cache.contracts import ModelCacheEntry

    volume = _volume()
    stale_entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model-a")),
        cachePath="models--org--model-a",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")],
    )
    synced_entry = ModelCacheEntry(
        clusterId="cluster-1",
        storageVolumeId=volume.id,
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model-b")),
        cachePath="models--org--model-b",
        status=ModelCacheEntryStatus.READY,
        nodeProgress=[
            NodeDownloadStatus(node="node-1", status="ready"),
            NodeDownloadStatus(node="node-2", status="ready"),
        ],
    )
    service._store.create(stale_entry)
    service._store.create(synced_entry)
    monkeypatch.setattr(service_module, "get_or_none_volume", lambda volume_id: volume)

    async def fake_missing_nodes(entry, volume):
        return ["node-2"] if entry.id == stale_entry.id else []

    monkeypatch.setattr(jobs_module, "missing_nodes", fake_missing_nodes)
    changed = await service.sync_all_nodes()
    assert [entry.id for entry in changed] == [stale_entry.id]
    assert service._store.get(stale_entry.id).status == ModelCacheEntryStatus.DOWNLOADING
    assert service._store.get(synced_entry.id).status == ModelCacheEntryStatus.READY


async def _async_result(value):
    return value


def test_cluster_token_source_uses_saved_cluster_secret(monkeypatch):
    import llm_d_bench.cluster.registry as registry

    monkeypatch.setattr(
        registry,
        "get_cluster",
        lambda _cid: SimpleNamespace(
            hf_token_secret_namespace="ns",  # noqa: S106 - namespace is not a credential
            hf_token_secret_name="llm-d-hf-token",  # noqa: S106
        ),
    )
    source = service_module._cluster_token_source("cluster-a")
    assert source.mode == TokenSourceMode.EXISTING_SECRET
    assert (source.namespace, source.name) == ("ns", "llm-d-hf-token")


def test_cluster_token_source_falls_back_when_cluster_has_none(monkeypatch):
    import llm_d_bench.cluster.registry as registry

    monkeypatch.setattr(
        registry, "get_cluster", lambda _cid: SimpleNamespace(hf_token_secret_namespace=None, hf_token_secret_name=None)
    )
    assert service_module._cluster_token_source("cluster-a").mode == TokenSourceMode.NONE
