"""Tests for the Storage HTTP API (calls route handlers directly, no TestClient)."""

import asyncio

import pytest
from fastapi import HTTPException

from llm_d_bench.storage import router as storage_router
from llm_d_bench.storage import service as storage_service
from llm_d_bench.storage.contracts import (
    LocalDiskSpec,
    StorageVolume,
    StorageVolumeCreateRequest,
    StorageVolumeKind,
    StorageVolumeStatus,
)
from llm_d_bench.storage.service import StorageVolumeInUseError, StorageVolumeService
from llm_d_bench.storage.store import StorageVolumeStore


@pytest.fixture()
def service(tmp_path, monkeypatch):
    store = StorageVolumeStore(tmp_path)
    svc = StorageVolumeService(store)
    monkeypatch.setattr(storage_router, "_service", svc)
    return svc


def _volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "status": StorageVolumeStatus.READY,
        "capacity": "100Gi",
        "readOnly": True,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
        "pvcName": "prism-storage-abc",
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


@pytest.mark.asyncio
async def test_list_volumes_filters_by_cluster_and_kind(service):
    v1 = _volume(clusterId="cluster-1")
    v2 = _volume(clusterId="cluster-2", name="other")
    service._store.create(v1)
    service._store.create(v2)

    result = await storage_router.list_volumes(cluster_id="cluster-1")
    assert [item["id"] for item in result["items"]] == [v1.id]

    result = await storage_router.list_volumes(kind="local-disk")
    assert len(result["items"]) == 2

    result = await storage_router.list_volumes(query="other")
    assert [item["id"] for item in result["items"]] == [v2.id]


@pytest.mark.asyncio
async def test_list_volumes_filters_by_purpose(service):
    v1 = _volume(purposes=["model-cache"])
    v2 = _volume(name="other-2")
    service._store.create(v1)
    service._store.create(v2)

    result = await storage_router.list_volumes(purpose="model-cache")
    assert [item["id"] for item in result["items"]] == [v1.id]


@pytest.mark.asyncio
async def test_get_volume_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.get_volume("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_volume_returns_payload(service):
    volume = _volume()
    service._store.create(volume)
    payload = await storage_router.get_volume(volume.id)
    assert payload["id"] == volume.id
    assert payload["inUseCount"] == 0


@pytest.mark.asyncio
async def test_list_volumes_reports_nodes_added_for_local_disk_drift(service, monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _volume(knownNodes=["node-1"])
    service._store.create(volume)

    result = await storage_router.list_volumes()
    assert result["items"][0]["nodesAdded"] == ["node-2"]

    payload = await storage_router.get_volume(volume.id)
    assert payload["nodesAdded"] == ["node-2"]


@pytest.mark.asyncio
async def test_acknowledge_volume_nodes_clears_drift(service, monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _volume(knownNodes=["node-1"])
    service._store.create(volume)

    payload = await storage_router.acknowledge_volume_nodes(volume.id)
    assert payload["nodesAdded"] == []
    assert service._store.get(volume.id).known_nodes == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_acknowledge_volume_nodes_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.acknowledge_volume_nodes("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_volume_resource_status_returns_live_kubernetes_statuses(service, monkeypatch):
    volume = _volume()
    service._store.create(volume)

    async def fake_statuses(volumes):
        assert volumes == [volume]
        return {
            volume.id: {
                "persistentVolumeClaims": [
                    {
                        "name": volume.pvc_name,
                        "namespace": "llm-d-bench-storage",
                        "phase": "Bound",
                        "capacity": "100Gi",
                        "accessModes": ["ReadWriteMany"],
                        "volumeName": volume.pvc_name,
                        "persistentVolume": {
                            "name": volume.pvc_name,
                            "namespace": None,
                            "phase": "Bound",
                            "capacity": "100Gi",
                            "accessModes": ["ReadWriteMany"],
                        },
                    }
                ],
            }
        }

    monkeypatch.setattr(storage_router, "storage_resource_statuses", fake_statuses)

    result = await storage_router.get_volume_resource_status(volume_ids=[volume.id, "missing"])

    assert result["items"][volume.id]["persistentVolumeClaims"][0]["phase"] == "Bound"


@pytest.mark.asyncio
async def test_create_volume_schedules_background_provisioning(service, monkeypatch):
    provisioned = asyncio.Event()

    async def fake_provision(volume_id):
        provisioned.set()

    monkeypatch.setattr(service, "provision", fake_provision)

    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="new-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    payload = await storage_router.create_volume(request)
    assert payload["status"] == "pending"
    await asyncio.wait_for(provisioned.wait(), timeout=1)


@pytest.mark.asyncio
async def test_create_volume_rejects_duplicate_name(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(service, "provision", fake_provision)
    service._store.create(_volume(name="qwen-cache"))

    request = StorageVolumeCreateRequest(
        clusterId="cluster-2",
        name="Qwen-Cache",  # different case, still a duplicate
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    response = await storage_router.create_volume(request)
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_create_volume_disambiguates_colliding_default_name(service, monkeypatch):
    provisioned = []

    async def fake_provision(volume_id):
        provisioned.append(volume_id)

    monkeypatch.setattr(service, "provision", fake_provision)
    service._store.create(_volume(name="storage-local-disk"))

    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="",  # blank -> auto-generated default collides with the existing volume
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    payload = await storage_router.create_volume(request)
    assert payload["name"] == "storage-local-disk-2"


@pytest.mark.asyncio
async def test_delete_volume_returns_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.delete_volume("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_volume_returns_409_when_in_use(service, monkeypatch):
    volume = _volume()
    service._store.create(volume)

    async def fake_delete(volume_id):
        raise StorageVolumeInUseError(["exec-1"])

    monkeypatch.setattr(service, "delete", fake_delete)

    response = await storage_router.delete_volume(volume.id)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_volume_returns_202_and_marks_deleting_on_success(service, monkeypatch):
    volume = _volume()
    service._store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: [])
    monkeypatch.setattr(storage_router, "list_model_cache_refs", lambda volume_id: [])
    finished = asyncio.Event()

    async def fake_finish_delete(volume_id, *, keep_model_files):
        finished.set()

    monkeypatch.setattr(service, "finish_delete", fake_finish_delete)

    payload = await storage_router.delete_volume(volume.id)
    assert payload["status"] == "deleting"
    assert service._store.get(volume.id) is not None  # still present -- finish_delete is a background task
    await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_list_storage_classes_and_nodes_delegate(monkeypatch):
    async def fake_classes(cluster_id):
        assert cluster_id == "cluster-1"
        return []

    async def fake_nodes(cluster_id):
        assert cluster_id == "cluster-1"
        return []

    monkeypatch.setattr(storage_router, "discover_storage_classes", fake_classes)
    monkeypatch.setattr(storage_router, "discover_nodes", fake_nodes)

    assert await storage_router.list_storage_classes(cluster_id="cluster-1") == {"items": []}
    assert await storage_router.list_nodes(cluster_id="cluster-1") == {"items": []}


@pytest.mark.asyncio
async def test_list_storage_classes_404_on_missing_cluster(monkeypatch):
    async def fake_classes(cluster_id):
        raise FileNotFoundError("cluster not found")

    monkeypatch.setattr(storage_router, "discover_storage_classes", fake_classes)
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.list_storage_classes(cluster_id="missing")
    assert exc_info.value.status_code == 404
