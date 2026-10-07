"""Tests for the Model Cache HTTP API (calls route handlers directly, no TestClient)."""

import asyncio

import pytest
from fastapi import HTTPException

from llm_d_bench.model_cache import router as model_cache_router
from llm_d_bench.model_cache.contracts import (
    HuggingFaceSource,
    ModelCacheCreateRequest,
    ModelCacheEntry,
    ModelCacheEntryStatus,
    ModelSource,
    ModelSourceKind,
    NodeDownloadStatus,
)
from llm_d_bench.model_cache.service import ModelCacheService
from llm_d_bench.model_cache.store import ModelCacheStore


@pytest.fixture()
def service(tmp_path, monkeypatch) -> ModelCacheService:
    svc = ModelCacheService(ModelCacheStore(tmp_path))
    monkeypatch.setattr(model_cache_router, "_service", svc)
    return svc


def _entry(**overrides) -> ModelCacheEntry:
    defaults = {
        "clusterId": "cluster-1",
        "storageVolumeId": "vol-1",
        "source": ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
        "cachePath": "models--org--model",
    }
    defaults.update(overrides)
    return ModelCacheEntry(**defaults)


@pytest.mark.asyncio
async def test_list_entries_filters_by_cluster_and_volume(service):
    e1 = _entry(clusterId="cluster-1", storageVolumeId="vol-1")
    e2 = _entry(clusterId="cluster-2", storageVolumeId="vol-2")
    service._store.create(e1)
    service._store.create(e2)

    result = await model_cache_router.list_entries(cluster_id="cluster-1")
    assert [item["id"] for item in result["items"]] == [e1.id]

    result = await model_cache_router.list_entries(storage_volume_id="vol-2")
    assert [item["id"] for item in result["items"]] == [e2.id]


@pytest.mark.asyncio
async def test_get_entry_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await model_cache_router.get_entry("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_entry_returns_payload(service):
    entry = _entry()
    service._store.create(entry)
    payload = await model_cache_router.get_entry(entry.id)
    assert payload["id"] == entry.id


@pytest.mark.asyncio
async def test_create_entry_schedules_background_provisioning(service, monkeypatch):
    provisioned = asyncio.Event()

    async def fake_provision(entry_id):
        provisioned.set()

    monkeypatch.setattr(service, "provision", fake_provision)

    async def fake_request_download(request):
        return _entry(storageVolumeId=request.storage_volume_id)

    monkeypatch.setattr(service, "request_download", fake_request_download)

    request = ModelCacheCreateRequest(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
    )
    payload = await model_cache_router.create_entry(request)
    assert payload["status"] == "pending"
    await asyncio.wait_for(provisioned.wait(), timeout=1)


@pytest.mark.asyncio
async def test_create_entry_returns_501_for_model_catalog(service, monkeypatch):
    from llm_d_bench.model_cache.service import ModelCatalogNotSupportedError

    async def fake_request_download(request):
        raise ModelCatalogNotSupportedError("not supported")

    monkeypatch.setattr(service, "request_download", fake_request_download)

    request = ModelCacheCreateRequest(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
    )
    response = await model_cache_router.create_entry(request)
    assert response.status_code == 501


@pytest.mark.asyncio
async def test_create_entry_returns_409_for_not_ready_volume(service, monkeypatch):
    async def fake_request_download(request):
        raise ValueError("storage volume vol-1 is not ready (status=pending)")

    monkeypatch.setattr(service, "request_download", fake_request_download)

    request = ModelCacheCreateRequest(
        clusterId="cluster-1",
        storageVolumeId="vol-1",
        source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
    )
    response = await model_cache_router.create_entry(request)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_retry_entry_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await model_cache_router.retry_entry("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_retry_entry_409_when_not_failed(service):
    entry = _entry(status=ModelCacheEntryStatus.READY)
    service._store.create(entry)
    response = await model_cache_router.retry_entry(entry.id)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_retry_entry_schedules_background_provisioning(service, monkeypatch):
    entry = _entry(
        status=ModelCacheEntryStatus.FAILED,
        nodeProgress=[NodeDownloadStatus(node="node-1", status="failed")],
    )
    service._store.create(entry)

    provisioned = asyncio.Event()

    async def fake_provision(entry_id):
        provisioned.set()

    monkeypatch.setattr(service, "provision", fake_provision)
    payload = await model_cache_router.retry_entry(entry.id)
    assert payload["status"] == "pending"
    await asyncio.wait_for(provisioned.wait(), timeout=1)


@pytest.mark.asyncio
async def test_sync_entry_nodes_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await model_cache_router.sync_entry_nodes("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_sync_entry_nodes_schedules_background_provisioning_when_missing_nodes(service, monkeypatch):
    entry = _entry(status=ModelCacheEntryStatus.READY, nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    service._store.create(entry)

    async def fake_sync_nodes(entry_id):
        saved = service._store.get(entry_id)
        saved.status = ModelCacheEntryStatus.DOWNLOADING
        return service._store.save(saved)

    monkeypatch.setattr(service, "sync_nodes", fake_sync_nodes)

    provisioned = asyncio.Event()

    async def fake_provision_new_nodes(entry_id):
        provisioned.set()

    monkeypatch.setattr(service, "provision_new_nodes", fake_provision_new_nodes)
    payload = await model_cache_router.sync_entry_nodes(entry.id)
    assert payload["status"] == "downloading"
    await asyncio.wait_for(provisioned.wait(), timeout=1)


@pytest.mark.asyncio
async def test_sync_entry_nodes_does_not_schedule_provisioning_when_nothing_to_sync(service, monkeypatch):
    entry = _entry(status=ModelCacheEntryStatus.READY, nodeProgress=[NodeDownloadStatus(node="node-1", status="ready")])
    service._store.create(entry)

    async def fake_sync_nodes(entry_id):
        return service._store.get(entry_id)  # unchanged: still READY

    monkeypatch.setattr(service, "sync_nodes", fake_sync_nodes)

    async def fail_provision_new_nodes(entry_id):
        raise AssertionError("provision_new_nodes should not be scheduled when nothing changed")

    monkeypatch.setattr(service, "provision_new_nodes", fail_provision_new_nodes)
    payload = await model_cache_router.sync_entry_nodes(entry.id)
    assert payload["status"] == "ready"


@pytest.mark.asyncio
async def test_sync_all_entry_nodes_schedules_provisioning_for_each_changed_entry(service, monkeypatch):
    changed_entry = _entry(status=ModelCacheEntryStatus.DOWNLOADING)
    service._store.create(changed_entry)

    async def fake_sync_all_nodes(*, cluster_id=None):
        return [changed_entry]

    monkeypatch.setattr(service, "sync_all_nodes", fake_sync_all_nodes)

    provisioned_ids = []

    async def fake_provision_new_nodes(entry_id):
        provisioned_ids.append(entry_id)

    monkeypatch.setattr(service, "provision_new_nodes", fake_provision_new_nodes)
    payload = await model_cache_router.sync_all_entry_nodes()
    assert [item["id"] for item in payload["items"]] == [changed_entry.id]
    await asyncio.sleep(0)  # let the scheduled background tasks run
    assert provisioned_ids == [changed_entry.id]


@pytest.mark.asyncio
async def test_delete_entry_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await model_cache_router.delete_entry("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_entry_schedules_background_deletion(service, monkeypatch):
    entry = _entry(status=ModelCacheEntryStatus.READY)
    service._store.create(entry)

    deleted = asyncio.Event()

    async def fake_provision_delete(entry_id):
        deleted.set()

    monkeypatch.setattr(service, "provision_delete", fake_provision_delete)
    payload = await model_cache_router.delete_entry(entry.id)
    assert payload["status"] == "deleting"
    await asyncio.wait_for(deleted.wait(), timeout=1)


@pytest.mark.asyncio
async def test_delete_entry_removes_never_ready_entry_without_scheduling_provision(service, monkeypatch):
    """A pending/downloading entry has nothing written to the shared volume
    yet, so it must be removable even if the volume can't currently be
    mounted (e.g. an NFS server rejecting the node); no provision_delete
    background task should be scheduled since there's nothing to provision.
    """
    entry = _entry(
        status=ModelCacheEntryStatus.DOWNLOADING, nodeProgress=[NodeDownloadStatus(node="*", status="downloading")]
    )
    service._store.create(entry)

    async def fail_if_called(entry_id):
        raise AssertionError("provision_delete should not be scheduled for a never-ready entry")

    monkeypatch.setattr(service, "provision_delete", fail_if_called)
    payload = await model_cache_router.delete_entry(entry.id)
    assert payload["status"] == "deleted"
    assert service._store.get(entry.id) is None


@pytest.mark.asyncio
async def test_get_entry_logs_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await model_cache_router.get_entry_logs("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_entry_logs_returns_logs(service, monkeypatch):
    entry = _entry()
    service._store.create(entry)

    async def fake_get_logs(entry_id, *, node=None):
        assert entry_id == entry.id
        return "some logs"

    monkeypatch.setattr(service, "get_logs", fake_get_logs)
    payload = await model_cache_router.get_entry_logs(entry.id)
    assert payload == {"logs": "some logs"}


@pytest.mark.asyncio
async def test_search_huggingface_models_returns_empty_for_blank_query():
    result = await model_cache_router.search_huggingface_models(query="  ")
    assert result == {"items": []}


@pytest.mark.asyncio
async def test_search_huggingface_models_maps_hub_payload(monkeypatch):
    async def fake_search(query, limit=20):
        assert query == "llama"
        return [{"id": "org/llama", "likes": 5, "downloads": 100, "pipeline_tag": "text-generation", "tags": ["a"]}]

    monkeypatch.setattr(model_cache_router.huggingface_hub, "search_models", fake_search)
    result = await model_cache_router.search_huggingface_models(query="llama")
    assert result["items"] == [
        {
            "repoId": "org/llama",
            "likes": 5,
            "downloads": 100,
            "pipelineTag": "text-generation",
            "tags": ["a"],
            "lastModified": None,
            "gated": False,
        }
    ]


@pytest.mark.asyncio
async def test_search_huggingface_models_maps_upstream_error(monkeypatch):
    async def fake_search(query, limit=20):
        raise model_cache_router.huggingface_hub.HuggingFaceHubError("boom")

    monkeypatch.setattr(model_cache_router.huggingface_hub, "search_models", fake_search)
    response = await model_cache_router.search_huggingface_models(query="llama")
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_get_huggingface_model_returns_detail(monkeypatch):
    async def fake_detail(repo_id):
        assert repo_id == "org/llama"
        return {"info": {"id": "org/llama", "tags": ["text-generation"]}, "readme": "# Llama"}

    monkeypatch.setattr(model_cache_router.huggingface_hub, "get_model_detail", fake_detail)
    payload = await model_cache_router.get_huggingface_model("org/llama")
    assert payload["repoId"] == "org/llama"
    assert payload["readme"] == "# Llama"
    assert payload["tags"] == ["text-generation"]


@pytest.mark.asyncio
async def test_get_huggingface_model_404_when_missing(monkeypatch):
    async def fake_detail(repo_id):
        raise model_cache_router.huggingface_hub.HuggingFaceModelNotFoundError("missing")

    monkeypatch.setattr(model_cache_router.huggingface_hub, "get_model_detail", fake_detail)
    with pytest.raises(HTTPException) as exc_info:
        await model_cache_router.get_huggingface_model("org/missing")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_huggingface_model_maps_upstream_error(monkeypatch):
    async def fake_detail(repo_id):
        raise model_cache_router.huggingface_hub.HuggingFaceHubError("boom")

    monkeypatch.setattr(model_cache_router.huggingface_hub, "get_model_detail", fake_detail)
    response = await model_cache_router.get_huggingface_model("org/llama")
    assert response.status_code == 502
